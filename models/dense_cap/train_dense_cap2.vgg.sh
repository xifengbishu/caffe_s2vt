#!/usr/bin/env bash

GPU_ID=5
WEIGHTS=\
./models/vggnet/VGG_ILSVRC_16_layers.caffemodel

./build/tools/caffe train \
    -solver ./dense_cap/dense_cap2_solver.vgg.prototxt \
    -weights $WEIGHTS \
    -gpu $GPU_ID
