r"""PyTorch Detection Training.

To run in a multi-gpu environment, use the distributed launcher::

    python -m torch.distributed.launch --nproc_per_node=$NGPU --use_env \
        train.py ... --world-size $NGPU

The default hyperparameters are tuned for training on 8 gpus and 2 images per gpu.
    --lr 0.02 --batch-size 2 --world-size 8
If you use different number of gpus, the learning rate should be changed to 0.02/8*$NGPU.

On top of that, for training Faster/Mask R-CNN, the default hyperparameters are
    --epochs 26 --lr-steps 16 22 --aspect-ratio-group-factor 3

Also, if you train Keypoint R-CNN, the default hyperparameters are
    --epochs 46 --lr-steps 36 43 --aspect-ratio-group-factor 3
Because the number of images is smaller in the person keypoint subset of COCO,
the number of epochs should be adapted so that we have the same number of iterations.
"""
import datetime
import os
import time
import random
import math
import sys
import numpy as np
from scipy.spatial.distance import pdist, squareform
import math
import pickle

import torchvision.transforms.functional as F
import torch
import torch.utils.data
from torch import nn
from math import exp
import torchvision
import torchvision.models.detection
import torchvision.models.detection.mask_rcnn
import torch.optim as optim
from torch.utils.data import DataLoader
import torch.optim.lr_scheduler as lr_scheduler
from torch.utils.data.sampler import SubsetRandomSampler


from detection.frcnn_la import fasterrcnn_resnet50_fpn_feature
from detection.retinanet_cal import retinanet_resnet50_fpn_cal
from detection.coco_utils import get_coco, get_coco_kp
from detection.group_by_aspect_ratio import GroupedBatchSampler, create_aspect_ratio_groups
from detection.engine import coco_evaluate, voc_evaluate
from detection import utils
from detection import transforms as T
from detection.train import *

from ll4al.data.sampler import SubsetSequentialSampler
from cald.cald_helper import *


def train_one_epoch(task_model, task_optimizer, data_loader, device, cycle, epoch, print_freq):
    task_model.train()
    metric_logger = utils.MetricLogger(delimiter="  ")
    metric_logger.add_meter('task_lr', utils.SmoothedValue(window_size=1, fmt='{value:.6f}'))
    header = 'Cycle:[{}] Epoch: [{}]'.format(cycle, epoch)

    task_lr_scheduler = None

    if epoch == 0:
        warmup_factor = 1. / 1000
        warmup_iters = min(1000, len(data_loader) - 1)

        task_lr_scheduler = utils.warmup_lr_scheduler(task_optimizer, warmup_iters, warmup_factor)

    for images, targets in metric_logger.log_every(data_loader, print_freq, header):
        images = list(image.to(device) for image in images)
        targets = [{k: v.to(device) for k, v in t.items()} for t in targets]

        task_loss_dict = task_model(images, targets)
        task_losses = sum(loss for loss in task_loss_dict.values())
        # reduce losses over all GPUs for logging purposes
        task_loss_dict_reduced = utils.reduce_dict(task_loss_dict)
        task_losses_reduced = sum(loss.cpu() for loss in task_loss_dict_reduced.values())
        task_loss_value = task_losses_reduced.item()
        if not math.isfinite(task_loss_value):
            print("Loss is {}, stopping training".format(task_loss_value))
            print(task_loss_dict_reduced)
            sys.exit(1)

        task_optimizer.zero_grad()
        task_losses.backward()
        task_optimizer.step()
        if task_lr_scheduler is not None:
            task_lr_scheduler.step()
        metric_logger.update(task_loss=task_losses_reduced)
        metric_logger.update(task_lr=task_optimizer.param_groups[0]["lr"])
    return metric_logger


def calcu_iou(A, B):
    '''
    calculate two box's iou
    '''
    width = min(A[2], B[2]) - max(A[0], B[0]) + 1
    height = min(A[3], B[3]) - max(A[1], B[1]) + 1
    if width <= 0 or height <= 0:
        return 0
    Aarea = (A[2] - A[0]) * (A[3] - A[1] + 1)
    Barea = (B[2] - B[0]) * (B[3] - B[1] + 1)
    iner_area = width * height
    return iner_area / (Aarea + Barea - iner_area)


def get_uncertainty(task_model, unlabeled_loader, aves=None):
    task_model.eval()
    with torch.no_grad():
        stability_all = []
        for images, _ in unlabeled_loader:
            torch.cuda.synchronize()
            # only support 1 batch size
            aug_images = []
            for image in images:
                output = task_model([F.to_tensor(image).cuda()])
                ref_boxes, prob_max, ref_labels = output[0]['boxes'], output[0]['prob_max'], output[0]['labels']
                if ref_boxes.shape[0] == 0:
                    stability_all.append(0.0)
                    break
                if len(ref_boxes) > 30:
                    inds = torch.topk(prob_max, 30)[1]
                    ref_boxes, prob_max, ref_labels = ref_boxes[inds], prob_max[inds], ref_labels[inds]
                stability_img = [0.0] * len(ref_boxes)
                U = torch.max(1 - prob_max).item()
                # print(U)
                for i in range(1, 7):
                    ga_image = GaussianNoise(image, i * 8)
                    aug_images.append(ga_image.cuda())
                    # draw_PIL_image(ga_image, ref_boxes, ref_labels, i)
                outputs = []
                for aug_image in aug_images:
                    outputs.append(task_model([aug_image])[0])
                for output in outputs:
                    boxes = output['boxes']
                    if len(boxes) == 0:
                        continue
                    i = 0
                    for ab in ref_boxes:
                        width = torch.min(ab[2], boxes[:, 2]) - torch.max(ab[0], boxes[:, 0])
                        height = torch.min(ab[3], boxes[:, 3]) - torch.max(ab[1], boxes[:, 1])
                        Aarea = (ab[2] - ab[0]) * (ab[3] - ab[1])
                        Barea = (boxes[:, 2] - boxes[:, 0]) * (boxes[:, 3] - boxes[:, 1])
                        iner_area = width * height
                        iou = iner_area / (Aarea + Barea - iner_area)
                        iou[width < 0] = 0.0
                        iou[height < 0] = 0.0
                        stability_img[i] += torch.max(iou).item()
                        i += 1
                stability_img = np.array(stability_img) / 6.0
                prob_max = prob_max.cpu().numpy()
                stability_img = np.sum(prob_max * stability_img) / np.sum(prob_max)
                stability_all.append(stability_img - U)
    return stability_all

def get_unlabeledset(unlabeled_loader, task_model):
    task_model.eval()
    with torch.no_grad():
        unlabeledset = []
        for images, _ in unlabeled_loader:
            for image in images:
                features = task_model([F.to_tensor(image).cuda()])  # Extract features using the model
                unlabeledset.append(features.detach().cpu().numpy())  # Detach, move to CPU, and convert to NumPy array
    # return np.concatenate(unlabeledset, axis=0)  # Concatenate features along the first dimension
    return np.array(unlabeledset)

def dist_cal(unlabeled_embeddings):
    print("size of the unlabeled embeddings before calculating the distance: ", unlabeled_embeddings.shape)
    dist_mat = squareform(pdist(unlabeled_embeddings, metric="cosine"))
    return dist_mat

def knei_dist(interd,fetch):
    num_nei = round(interd.shape[0]/fetch)
    knei_dist = []
    for i in range(np.shape(interd)[0]):
        temp_dist = np.sort(interd[i][:])
        knei_dist.append(np.mean(temp_dist[:num_nei]))
        dth = np.mean(knei_dist)
    return dth

def diversity_select(fetchsize, embedding_unlabeled, bs, uncertainty_score):
    idx = []
    nb = round(np.shape(embedding_unlabeled)[0]/bs)
    for b in range(nb):
        embedding_unlabeled_batch = embedding_unlabeled[b*bs:(b+1)*bs][:]
        interd = dist_cal(embedding_unlabeled_batch)
        dth = knei_dist(interd, round(fetchsize/nb))
        priority = uncertainty_score
        # print(priority)
        for i in range(round(fetchsize/nb)):
          top_idx = np.argmax(priority)
          idx.append(top_idx)
          neighbordist = interd[top_idx][:]
          neighboridx = np.where(neighbordist <= dth)[0]
          priority[top_idx] = priority[top_idx] / (1 + 20*np.sum(priority[neighboridx]))
          priority[neighboridx] = priority[neighboridx] / (1 + 20*np.sum(priority[neighboridx]))
    return idx


def main(args):
    torch.cuda.set_device(0)
    random.seed(0)
    torch.manual_seed(0)
    torch.cuda.manual_seed(0)
    torch.cuda.manual_seed_all(0)
    print(args)

    device = torch.device(args.device)

    # Data loading code
    print("Loading data")

    if 'voc2007' in args.dataset:
        dataset, num_classes = get_dataset(args.dataset, "trainval", get_transform(train=True), args.data_path)
        dataset_aug, _ = get_dataset(args.dataset, "trainval", None, args.data_path)
        dataset_test, _ = get_dataset(args.dataset, "test", get_transform(train=False), args.data_path)
    else:
        dataset, num_classes = get_dataset(args.dataset, "train", get_transform(train=True), args.data_path)
        dataset_aug, _ = get_dataset(args.dataset, "train", None, args.data_path)
        dataset_test, _ = get_dataset(args.dataset, "val", get_transform(train=False), args.data_path)

    print("Creating data loaders")
    num_images = len(dataset)
    if 'voc' in args.dataset:
        init_num = 500
        budget_num = 500
        if 'retina' in args.model:
            init_num = 1000
            budget_num = 500
    else:
        init_num = 5000
        budget_num = 1000
    indices = list(range(num_images))
    random.shuffle(indices)
    labeled_set = indices[:init_num]
    unlabeled_set = indices[init_num:]
    train_sampler = SubsetRandomSampler(labeled_set)
    test_sampler = torch.utils.data.SequentialSampler(dataset_test)
    data_loader_test = DataLoader(dataset_test, batch_size=1, sampler=test_sampler, num_workers=args.workers,
                                  collate_fn=utils.collate_fn)
    for cycle in range(args.cycles):
        if args.aspect_ratio_group_factor >= 0:
            group_ids = create_aspect_ratio_groups(dataset, k=args.aspect_ratio_group_factor)
            train_batch_sampler = GroupedBatchSampler(train_sampler, group_ids, args.batch_size)
        else:
            train_batch_sampler = torch.utils.data.BatchSampler(train_sampler, args.batch_size, drop_last=True)

        data_loader = torch.utils.data.DataLoader(dataset, batch_sampler=train_batch_sampler, num_workers=args.workers,
                                                  collate_fn=utils.collate_fn)

        print("Creating model")
        if 'voc' in args.dataset:
            if 'faster' in args.model:
                task_model = fasterrcnn_resnet50_fpn_feature(num_classes=num_classes, min_size=600, max_size=1000)
            elif 'retina' in args.model:
                task_model = retinanet_resnet50_fpn_cal(num_classes=num_classes, min_size=600, max_size=1000)
        else:
            if 'faster' in args.model:
                task_model = fasterrcnn_resnet50_fpn_feature(num_classes=num_classes, min_size=800, max_size=1333)
            elif 'retina' in args.model:
                task_model = retinanet_resnet50_fpn_cal(num_classes=num_classes, min_size=800, max_size=1333)
        task_model.to(device)
        if not args.init and cycle == 0 and args.skip:
            if 'faster' in args.model:
                checkpoint = torch.load(os.path.join(args.first_checkpoint_path,
                                                     '{}_frcnn_1st.pth'.format(args.dataset)), map_location='cpu')
            elif 'retina' in args.model:
                checkpoint = torch.load(os.path.join(args.first_checkpoint_path,
                                                     '{}_retinanet_1st.pth'.format(args.dataset)), map_location='cpu')
            task_model.load_state_dict(checkpoint['model'])
            if args.test_only:
                if 'coco' in args.dataset:
                    coco_evaluate(task_model, data_loader_test)
                elif 'voc' in args.dataset:
                    voc_evaluate(task_model, data_loader_test, args.dataset, False, path=args.results_path)
                return
            print("Getting stability")
            random.shuffle(unlabeled_set)
            if 'coco' in args.dataset:
                subset = unlabeled_set[:5000]
            else:
                subset = unlabeled_set
                
            unlabeled_loader = DataLoader(dataset_aug, batch_size=1, sampler=SubsetSequentialSampler(subset),
                                          num_workers=args.workers, pin_memory=True, collate_fn=utils.collate_fn)
                      
            unlabeledset = get_unlabeledset(unlabeled_loader, task_model)

        # Start active learning cycles training
        if args.test_only:
            if 'coco' in args.dataset:
                coco_evaluate(task_model, data_loader_test)
            elif 'voc' in args.dataset:
                voc_evaluate(task_model, data_loader_test, args.dataset)
            return
        print("Start training")
        start_time = time.time()
        for epoch in range(args.start_epoch, args.total_epochs):
            train_one_epoch(task_model, task_optimizer, data_loader, device, cycle, epoch, args.print_freq)
            task_lr_scheduler.step()
            # evaluate after pre-set epoch
            if (epoch + 1) == args.total_epochs:
                if 'coco' in args.dataset:
                    coco_evaluate(task_model, data_loader_test)
                elif 'voc' in args.dataset:
                    voc_evaluate(task_model, data_loader_test, args.dataset, path=args.results_path)
        random.shuffle(unlabeled_set)
        if 'coco' in args.dataset:
            subset = unlabeled_set[:5000]
        else:
            subset = unlabeled_set
        unlabeled_loader = DataLoader(dataset_aug, batch_size=1, sampler=SubsetSequentialSampler(subset),
                                      num_workers=args.workers, pin_memory=True, collate_fn=utils.collate_fn)
        unlabeledset = get_unlabeledset(unlabeled_loader, task_model)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description=__doc__)

    parser.add_argument('-p', '--data-path', default='/data/yuweiping/coco/', help='dataset path')
    parser.add_argument('--dataset', default='voc2007', help='dataset')
    parser.add_argument('--model', default='fasterrcnn_resnet50_fpn', help='model')
    parser.add_argument('--device', default='cuda', help='device')
    parser.add_argument('-b', '--batch-size', default=4, type=int,
                        help='images per gpu, the total batch size is $NGPU x batch_size')
    parser.add_argument('-cp', '--first-checkpoint-path', default='/data/yuweiping/coco/',
                        help='path to save checkpoint of first cycle')
    parser.add_argument('--task_epochs', default=20, type=int, metavar='N',
                        help='number of total epochs to run')
    parser.add_argument('-e', '--total_epochs', default=20, type=int, metavar='N',
                        help='number of total epochs to run')
    parser.add_argument('--cycles', default=7, type=int, metavar='N',
                        help='number of cycles epochs to run')
    parser.add_argument('-j', '--workers', default=4, type=int, metavar='N',
                        help='number of data loading workers (default: 4)')
    parser.add_argument('--lr', default=0.0025, type=float,
                        help='initial learning rate, 0.02 is the default value for training '
                             'on 8 gpus and 2 images_per_gpu')
    parser.add_argument('--ll-weight', default=0.5, type=float,
                        help='ll loss weight')
    parser.add_argument('--momentum', default=0.9, type=float, metavar='M',
                        help='momentum')
    parser.add_argument('--wd', '--weight-decay', default=1e-4, type=float,
                        metavar='W', help='weight decay (default: 1e-4)',
                        dest='weight_decay')
    parser.add_argument('--lr-step-size', default=8, type=int, help='decrease lr every step-size epochs')
    parser.add_argument('--lr-steps', default=[16, 19], nargs='+', type=int, help='decrease lr every step-size epochs')
    parser.add_argument('--lr-gamma', default=0.1, type=float, help='decrease lr by a factor of lr-gamma')
    parser.add_argument('--print-freq', default=1000, type=int, help='print frequency')
    parser.add_argument('--output-dir', default=None, help='path where to save')
    parser.add_argument('--resume', default='', help='resume from checkpoint')
    parser.add_argument('-rp', '--results-path', default='results',
                        help='path to save detection results (only for voc)')
    parser.add_argument('--start_epoch', default=0, type=int, help='start epoch')
    parser.add_argument('--aspect-ratio-group-factor', default=3, type=int)
    parser.add_argument('-i', "--init", dest="init", help="if use init sample", action="store_true")
    parser.add_argument("--test-only", dest="test_only", help="Only test the model", action="store_true")
    parser.add_argument('-s', "--skip", dest="skip", help="Skip first cycle and use pretrained model to save time",
                        action="store_true")
    parser.add_argument("--pretrained", dest="pretrained", help="Use pre-trained models from the modelzoo",
                        action="store_true")
    # distributed training parameters
    parser.add_argument('--world-size', default=1, type=int,
                        help='number of distributed processes')
    parser.add_argument('--dist-url', default='env://', help='url used to set up distributed training')

    args = parser.parse_args()

    if args.output_dir:
        utils.mkdir(args.output_dir)

    main(args)
