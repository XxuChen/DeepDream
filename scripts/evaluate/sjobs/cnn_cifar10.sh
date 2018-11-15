#!/bin/bash
#SBATCH --gres=gpu:2        # request GPU "generic resource"
#SBATCH --cpus-per-task=6   # maximum CPU cores per GPU request: 6 on Cedar, 16 on Graham.
#SBATCH --mem=10000M        # memory per node
#SBATCH --time=0-02:00      # time (DD-HH:MM)
#SBATCH --output=cnn_cifar10-%N-%j.out  # %N for node name, %j for jobID

source ~/tfp363/bin/activate

python ~/DeepDream/experiment.py --mode=evaluate --data_dir=/home/xuc/DeepDream/data/cifar-10-batches-mat/ --dataset=cifar10 --max_epochs=1 --summary_dir=/home/xuc/projects/def-sageev/xuc/nov_13/cnn_cifar10_ep1000_tbs_200 --total_batch_size=200 --model=cnn
