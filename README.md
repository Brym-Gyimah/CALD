#Active Learning for Object Detection
## Requirement
Create a conda environment with python 3.7 and activate the environment
- conda create -n CALD python=3.7 -y
Install pytorch 
- pytorch>=1.7.1
- torchvision=0.8.2

(option if you want to get class-wise results of coco)
- pip install mmcv-full==1.0.4 -f https://download.openmmlab.com/mmcv/dist/cu110/torch1.7.1/index.html
- pip install cython==0.29.33
- pip install pycocotools==2.0.2
- pip install terminaltables==3.1.0

## Quick start
```
Single-GPU
python ls_c_train.py --dataset voc2012 --data-path your_data_path --model faster

Multi-GPU
python -m torch.distributed.launch --nproc_per_node=2 --use_env ls_c_train.py --dataset 'coco' --data-path './data/coco/' --model faster --first-checkpoint-path './checkpoint-path/'
``` 
