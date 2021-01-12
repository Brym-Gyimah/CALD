import pickle

import datetime
import os
import time
import random
import math
import sys
import numpy as np
import math
import scipy.stats
from scipy.stats import gaussian_kde
import matplotlib.pyplot as plt
import pickle

import torch
import torch.utils.data
from torch import nn
import torchvision
import torchvision.models.detection
import torchvision.models.detection.mask_rcnn
import torch.optim as optim
from torch.utils.data import DataLoader
import torch.optim.lr_scheduler as lr_scheduler
from torch.utils.data.sampler import SubsetRandomSampler, SequentialSampler
import torchvision.transforms.functional as F

from detection.coco_utils import get_coco, get_coco_kp
from detection.group_by_aspect_ratio import GroupedBatchSampler, create_aspect_ratio_groups
from detection.engine import coco_evaluate, voc_evaluate
from detection import utils
from detection import transforms as T
from detection.train import *
from torchvision.models.detection.faster_rcnn import fasterrcnn_resnet50_fpn
from cal4od.cal4od_helper import *
from ll4al.data.sampler import SubsetSequentialSampler
from detection.frcnn_la import fasterrcnn_resnet50_fpn_feature


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
        consistency_all = []
        mean_all = []
        for images, _ in unlabeled_loader:
            torch.cuda.synchronize()
            # only support 1 batch size
            aug_images = []
            aug_boxes = []
            for image in images:
                output = task_model([F.to_tensor(image).cuda()])
                ref_boxes, prob_max, ref_scores_cls, ref_labels = output[0]['boxes'], output[0][
                    'prob_max'], output[0]['scores_cls'], output[0]['labels']
                if output[0]['boxes'].shape[0] == 0:
                    consistency_all.append(0.0)
                    break
                U = torch.max(1 - prob_max).item()
                # start augment
                # image = SaltPepperNoise(image, 0.05)
                flip_image, flip_boxes = HorizontalFlip(image, ref_boxes)
                aug_images.append(flip_image.cuda())
                aug_boxes.append(flip_boxes.cuda())
                # draw_PIL_image(flip_image, flip_boxes, ref_labels, '_1')
                # color_swap_image = ColorSwap(image)
                # aug_images.append(color_swap_image.cuda())
                # aug_boxes.append(reference_boxes)
                # draw_PIL_image(color_swap_image, reference_boxes, reference_labels, 'color_swap')
                # for i in range(2, 6):
                #     color_adjust_image = ColorAdjust(image, i)
                #     aug_images.append(color_adjust_image.cuda())
                #     aug_boxes.append(reference_boxes)
                #     draw_PIL_image(color_adjust_image, reference_boxes, reference_labels, i)
                # for i in range(1, 7):
                #     sp_image = SaltPepperNoise(image, i * 0.05)
                #     aug_images.append(sp_image.cuda())
                #     aug_boxes.append(ref_boxes)
                #     draw_PIL_image(sp_image, ref_boxes, ref_labels, i)
                ga_image = GaussianNoise(image, 8)
                aug_images.append(ga_image.cuda())
                aug_boxes.append(ref_boxes.cuda())
                cutout_image = cutout(image, ref_boxes, ref_labels)
                aug_images.append(cutout_image.cuda())
                aug_boxes.append(ref_boxes)
                # draw_PIL_image(cutout_image, ref_boxes, ref_labels, '_2')
                # flip_cutout_image = cutout(flip_image.cuda(), flip_boxes.cuda(), ref_labels)
                # aug_images.append(flip_cutout_image.cuda())
                # aug_boxes.append(flip_boxes.cuda())
                resize_image, resize_boxes = resize(image, ref_boxes, 0.8)
                aug_images.append(resize_image.cuda())
                aug_boxes.append(resize_boxes)
                # # draw_PIL_image(resize_image, resize_boxes, ref_labels, '_3')
                resize_image, resize_boxes = resize(image, ref_boxes, 1.2)
                aug_images.append(resize_image.cuda())
                aug_boxes.append(resize_boxes)
                # draw_PIL_image(resize_image, resize_boxes, ref_labels, '_4')
                # rot_image, rot_boxes = rotate(flip_image, flip_boxes, 10)
                # aug_images.append(rot_image.cuda())
                # aug_boxes.append(rot_boxes)
                # draw_PIL_image(rot_image, ref_boxes, ref_labels, 1)
                # rot_image, rot_boxes = rotate(flip_image, flip_boxes, -10)
                # aug_images.append(rot_image.cuda())
                # aug_boxes.append(rot_boxes)
                # draw_PIL_image(rot_image, ref_boxes, ref_labels, 2)
                outputs = []
                for aug_image in aug_images:
                    outputs.append(task_model([aug_image])[0])
                consistency_aug = []
                mean_aug = []
                for output, aug_box, aug_image in zip(outputs, aug_boxes, aug_images):
                    consistency_img = 1.0
                    mean_img = []
                    boxes, scores_cls, pm, labels = output['boxes'], output['scores_cls'], output['prob_max'], output[
                        'labels']
                    if len(boxes) == 0:
                        consistency_aug.append(0.0)
                        mean_aug.append(0.0)
                        continue
                    for ab, ref_score_cls, ref_pm in zip(aug_box, ref_scores_cls, prob_max):
                        width = torch.min(ab[2], boxes[:, 2]) - torch.max(ab[0], boxes[:, 0])
                        height = torch.min(ab[3], boxes[:, 3]) - torch.max(ab[1], boxes[:, 1])
                        Aarea = (ab[2] - ab[0]) * (ab[3] - ab[1])
                        Barea = (boxes[:, 2] - boxes[:, 0]) * (boxes[:, 3] - boxes[:, 1])
                        iner_area = width * height
                        iou = iner_area / (Aarea + Barea - iner_area)
                        iou[width < 0] = 0.0
                        iou[height < 0] = 0.0
                        p = ref_score_cls.cpu().numpy()
                        q = scores_cls[torch.argmax(iou)].cpu().numpy()
                        m = (p + q) / 2
                        js = 0.5 * scipy.stats.entropy(p, m) + 0.5 * scipy.stats.entropy(q, m)
                        if js < 0:
                            js = 0
                        consistency_img = min(consistency_img, torch.abs(
                            torch.max(iou) + 0.5 * (1 - js) * (ref_pm + pm[torch.argmax(iou)]) - 1.1).item())
                        mean_img.append(torch.abs(
                            torch.max(iou) + 0.5 * (1 - js) * (ref_pm + pm[torch.argmax(iou)])).item())
                        continue
                    consistency_aug.append(consistency_img)
                    mean_aug.append(np.mean(mean_img))
                    continue
                consistency_all.append(np.mean(consistency_aug))
                mean_all.append(mean_aug)
                continue
    mean_aug = np.mean(mean_all, axis=0)
    print(mean_aug)
    return consistency_all


def main(args):
    device = torch.device(args.device)

    if 'voc2007' in args.dataset:
        dataset, num_classes = get_dataset(args.dataset, "trainval", get_transform(train=True), args.data_path)
        dataset_aug, _ = get_dataset(args.dataset, "trainval", None, args.data_path)
        dataset_test, _ = get_dataset(args.dataset, "test", get_transform(train=False), args.data_path)
    else:
        dataset, num_classes = get_dataset(args.dataset, "train", get_transform(train=True), args.data_path)
        dataset_aug, _ = get_dataset(args.dataset, "train", None, args.data_path)
        dataset_test, _ = get_dataset(args.dataset, "val", get_transform(train=False), args.data_path)
    if 'voc' in args.dataset:
        task_model = fasterrcnn_resnet50_fpn_feature(num_classes=num_classes, min_size=600, max_size=1000)
    else:
        task_model = fasterrcnn_resnet50_fpn_feature(num_classes=num_classes, min_size=800, max_size=1333)
    task_model.to(device)
    checkpoint = torch.load(os.path.join(args.first_checkpoint_path, '{}_frcnn_1st.pth'.format(args.dataset)),
                            map_location='cpu')
    task_model.load_state_dict(checkpoint['model'])
    print("Getting stability")
    for cycle in range(0, 7):
        file = open('/home/lmy/ywp/code/active_learning_for_object_detection/vis/lt_c_{}.pkl'.format(cycle), 'rb')
        lt_c_set = pickle.load(file)
        file = open('/home/lmy/ywp/code/active_learning_for_object_detection/vis/cal4of_{}.pkl'.format(cycle), 'rb')
        cal_set = pickle.load(file)
        # a = [x for x in lt_c_set if x in cal_set]
        # b = [y for y in (lt_c_set + cal_set) if y not in a]
        _cal_set = [x for x in cal_set if x not in lt_c_set]  # 在list1列表中而不在list2列表中
        _lt_c_set = [y for y in lt_c_set if y not in cal_set]
        print(len(_cal_set), len(_lt_c_set))
        labeled_loader = DataLoader(dataset_aug, batch_size=1, sampler=SubsetSequentialSampler(_cal_set),
                                    num_workers=args.workers, pin_memory=True, collate_fn=utils.collate_fn)
        uncertainty = get_uncertainty(task_model, labeled_loader)
        print('cal4od:{}'.format(np.mean(uncertainty)))
        labeled_loader = DataLoader(dataset_aug, batch_size=1, sampler=SubsetSequentialSampler(_lt_c_set),
                                    num_workers=args.workers, pin_memory=True, collate_fn=utils.collate_fn)
        uncertainty = get_uncertainty(task_model, labeled_loader)
        print('lt_c:{}'.format(np.mean(uncertainty)))


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description=__doc__)

    parser.add_argument('-p', '--data-path', default='/home/lmy/ywp/data/coco/', help='dataset path')
    parser.add_argument('--dataset', default='coco', help='dataset')
    parser.add_argument('--model', default='fasterrcnn_resnet50_fpn', help='model')
    parser.add_argument('--device', default='cuda', help='device')
    parser.add_argument('-b', '--batch-size', default=2, type=int,
                        help='images per gpu, the total batch size is $NGPU x batch_size')
    parser.add_argument('-cp', '--first-checkpoint-path', default='/home/lmy/ywp/code/basemodel/',
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
