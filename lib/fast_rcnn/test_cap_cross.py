# --------------------------------------------------------
# Fast R-CNN
# Copyright (c) 2015 Microsoft
# Licensed under The MIT License [see LICENSE for details]
# Written by Ross Girshick
# --------------------------------------------------------

"""Test a Fast R-CNN network on an imdb (image database)."""

from fast_rcnn.config import cfg, get_output_dir
from fast_rcnn.bbox_transform import clip_boxes, bbox_transform_inv
import argparse
from utils.timer import Timer
import numpy as np
import math
import cv2
import caffe
from fast_rcnn.nms_wrapper import nms
import json
from utils.blob import im_list_to_blob
import os
import sys
sys.path.append('models/dense_cap/')
from run_experiment_vgg_vg import gt_region_merge, get_bbox_coord
from vg_to_hdf5_data import *
#sys.path.add('examples/coco-caption')
#import
COCO_EVAL_PATH = 'coco-caption/'
sys.path.append(COCO_EVAL_PATH)
from pycocoevalcap.vg_eval import VgEvalCap
eps = 1e-10
DEBUG=False

def _get_image_blob(im):
    """Converts an image into a network input.

    Arguments:
        im (ndarray): a color image in BGR order

    Returns:
        blob (ndarray): a data blob holding an image pyramid
        im_scale_factors (list): list of image scales (relative to im) used
            in the image pyramid
    """
    im_orig = im.astype(np.float32, copy=True)
    im_orig -= cfg.PIXEL_MEANS

    im_shape = im_orig.shape
    im_size_min = np.min(im_shape[0:2])
    im_size_max = np.max(im_shape[0:2])

    processed_ims = []
    im_scale_factors = []

    for target_size in cfg.TEST.SCALES:
        im_scale = float(target_size) / float(im_size_min)
        # Prevent the biggest axis from being more than MAX_SIZE
        if np.round(im_scale * im_size_max) > cfg.TEST.MAX_SIZE:
            im_scale = float(cfg.TEST.MAX_SIZE) / float(im_size_max)
        im = cv2.resize(im_orig, None, None, fx=im_scale, fy=im_scale,
                        interpolation=cv2.INTER_LINEAR)
        im_scale_factors.append(im_scale)
        processed_ims.append(im)

    # Create a blob to hold the input images
    blob = im_list_to_blob(processed_ims)

    return blob, np.array(im_scale_factors)

def _get_rois_blob(im_rois, im_scale_factors):
    """Converts RoIs into network inputs.

    Arguments:
        im_rois (ndarray): R x 4 matrix of RoIs in original image coordinates
        im_scale_factors (list): scale factors as returned by _get_image_blob

    Returns:
        blob (ndarray): R x 5 matrix of RoIs in the image pyramid
    """
    rois, levels = _project_im_rois(im_rois, im_scale_factors)
    rois_blob = np.hstack((levels, rois))
    return rois_blob.astype(np.float32, copy=False)

def _project_im_rois(im_rois, scales):
    """Project image RoIs into the image pyramid built by _get_image_blob.

    Arguments:
        im_rois (ndarray): R x 4 matrix of RoIs in original image coordinates
        scales (list): scale factors as returned by _get_image_blob

    Returns:
        rois (ndarray): R x 4 matrix of projected RoI coordinates
        levels (list): image pyramid levels used by each projected RoI
    """
    im_rois = im_rois.astype(np.float, copy=False)

    if len(scales) > 1:
        widths = im_rois[:, 2] - im_rois[:, 0] + 1
        heights = im_rois[:, 3] - im_rois[:, 1] + 1

        areas = widths * heights
        scaled_areas = areas[:, np.newaxis] * (scales[np.newaxis, :] ** 2)
        diff_areas = np.abs(scaled_areas - 224 * 224)
        levels = diff_areas.argmin(axis=1)[:, np.newaxis]
    else:
        levels = np.zeros((im_rois.shape[0], 1), dtype=np.int)

    rois = im_rois * scales[levels]

    return rois, levels

def _get_blobs(im, rois):
    """Convert an image and RoIs within that image into network inputs."""
    blobs = {'data' : None, 'rois' : None}
    blobs['data'], im_scale_factors = _get_image_blob(im)
    if not cfg.TEST.HAS_RPN:
        blobs['rois'] = _get_rois_blob(rois, im_scale_factors)
    return blobs, im_scale_factors
def _greedy_search(embed_net, recurrent_net, offset_net, recurrent_args, offset_args, proposal_n, max_timestep = 12):
    """Do greedy search to find the regions and captions"""
    # Data preparation


    pred_captions = [None] * proposal_n
    pred_logprobs = [0.0] * proposal_n
    pred_rois = np.zeros((proposal_n, 5))

   
    recurrent_args['cont_sentence'] = np.zeros((1,proposal_n))
    im_feats = recurrent_args['image_features']
    # remove the first dimension
    recurrent_args['image_features'] = im_feats.reshape(im_feats.shape[1:])
    input_sentence = np.zeros((1,proposal_n)) # start with EOS
    # reshape blobs
    for k, v in recurrent_args.iteritems():
        recurrent_net.blobs[k].reshape(*(v.shape))

    recurrent_net.blobs['input_features'].reshape(*(recurrent_args['offset_features'].shape))
    
    for k, v in offset_args.iteritems():
        offset_net.blobs[k].reshape(*(v.shape))
    offset_net.blobs['bbox_pred'].reshape(1,offset_args['rois'].shape[0],4)
    
    embed_net.blobs['input_sentence'].reshape(1, proposal_n)

    for step in xrange(max_timestep):
        embed_out = embed_net.forward(input_sentence=input_sentence)
        recurrent_args['input_features'] = embed_out['embedded_sentence']
        blobs_out = recurrent_net.forward(**recurrent_args)

        word_probs = blobs_out['probs'].copy()
        bbox_pred = blobs_out['bbox_pred']
        #suppress <unk> tag
        #word_probs[:,:,1] = 0
        best_words = word_probs.argmax(axis = 2).reshape(proposal_n)
        
        input_sentence[:] = best_words

        recurrent_args['cont_sentence'][:] = 1

        offset_args['bbox_pred'] = bbox_pred
        blobs_out_offset = offset_net.forward(**offset_args)
        recurrent_args['offset_features'][...] = blobs_out_offset['image_features']
        offset_args['rois'][...] = offset_net.blobs['rois_offset'].data
        for i, w in zip(range(proposal_n), best_words):
            if not pred_captions[i]:
                pred_captions[i] = [w]
                pred_logprobs[i] = math.log(word_probs[0,i,w])
            elif pred_captions[i][-1] != 0:
                pred_captions[i].append(w)
                pred_logprobs[i] += math.log(word_probs[0,i,w])
                pred_rois[i,:] = offset_args['rois'][i,:]

    return pred_captions, pred_rois, pred_logprobs

def im_detect(feature_net, embed_net, recurrent_net, offset_net, im, boxes=None):
    """Detect object classes in an image given object proposals.

    Arguments:
        feature_net (caffe.Net): CNN model for extracting features
        recurrent_net (caffe.Net): Recurrent model for generating captions and locations
        im (ndarray): color image to test (in BGR order)
        boxes (ndarray): R x 4 array of object proposals or None (for RPN)

    Returns:
        scores (ndarray): R x 1 array of object class scores 
        boxes_seq (list): length R list of caption_n x 4 array of predicted bounding boxes
        captions (list): length R list of length caption_n list of word tokens (captions)
    """
    # Previously:
    # 1. forward pass of one image
    # 2. get rois, bbox score and bbox prediction
    # Now:
    # 1. forward pass of one image --> image features and a list of proposals (rois)
    # 2. for each proposal, do greedy search, which is the same way as DenseCap
    # 
    # for bbox unnormalization
    # TODO: put it in a more organized way
    bbox_mean = np.array([0,0,0,0]).reshape((1,4))
    bbox_stds = np.array([0.1, 0.1, 0.2, 0.2]).reshape((1,4))

    blobs, im_scales = _get_blobs(im, boxes)
    assert len(im_scales) == 1, "Only single-image batch implemented"
    im_blob = blobs['data']
    blobs['im_info'] = np.array(
        [[im_blob.shape[2], im_blob.shape[3], im_scales[0]]],
        dtype=np.float32)

    # reshape network inputs
    feature_net.blobs['data'].reshape(*(blobs['data'].shape))
    feature_net.blobs['im_info'].reshape(*(blobs['im_info'].shape))
    
    feature_net.forward(data = im_blob, im_info = blobs['im_info'])
    region_features = feature_net.blobs['region_features'].data.copy()
    rois = feature_net.blobs['rois'].data.copy()
    conv_features = feature_net.blobs['conv5_3'].data.copy()
    #boxes = rois[:, 1:5] / im_scales[0]
    proposal_n = rois.shape[0]
    feat_args = {'image_features': region_features, 'offset_features': region_features.copy()}
    offset_args = {'rois':rois, 'im_info':blobs['im_info'], 'conv_features':conv_features}
    # use rpn scores, combine with caption score later
    scores = feature_net.blobs['cls_probs'].data[:,1].copy()
    #print recurrent_net.params['lstm1']
    

    captions, pred_rois, logprobs = _greedy_search(embed_net, recurrent_net, \
        offset_net, feat_args, offset_args, proposal_n)
   
    pred_boxes = pred_rois[:, 1:5] / im_scales[0]
    
    return scores, pred_boxes, captions

def vis_detections(im_path, im, captions, dets, thresh=0.6, save_path='vis'):
    """Visual debugging of detections by saving images with detected bboxes."""
    #add html generation for better visualization

    if not os.path.exists(save_path+'/images'):
                os.makedirs(save_path+'/images')
    im_name = im_path.split('/')[-1][:-4]
    page = open(os.path.join(save_path, im_name + '.html'),'w')
    page.write('<hr><h2>Image and the region captions</h2>')
    for i in xrange(dets.shape[0]):
        bbox = dets[i, :4]
        score = dets[i, -1]
        caption = captions[i]
        if score > thresh:
            im_new = np.copy(im)
            
            cv2.rectangle(im_new, (bbox[0],bbox[1]), (bbox[2],bbox[3]), (255,0,0), 2)
            im_rel_path = 'images/%s_%d.jpg' % (im_name, i)
            cv2.imwrite('%s/%s' % (save_path, im_rel_path), im_new)
            page.write('<div style=\'border: 2px solid; width:166px; height:360px; display:inline-table\'>')
            page.write('<image width="260" height = "260" src=\'%s\'></image><br> <hr><label> %s </label></div>' % (im_rel_path, caption))
    page.write('<hr>')
    page.close() 
        
        
            

def apply_nms(all_boxes, thresh):
    """Apply non-maximum suppression to all predicted boxes output by the
    test_net method.
    """
    num_classes = len(all_boxes)
    num_images = len(all_boxes[0])
    nms_boxes = [[[] for _ in xrange(num_images)]
                 for _ in xrange(num_classes)]
    for cls_ind in xrange(num_classes):
        for im_ind in xrange(num_images):
            dets = all_boxes[cls_ind][im_ind]
            if dets == []:
                continue
            # CPU NMS is much faster than GPU NMS when the number of boxes
            # is relative small (e.g., < 10k)
            # TODO(rbg): autotune NMS dispatch
            keep = nms(dets, thresh, force_cpu=True)
            if len(keep) == 0:
                continue
            nms_boxes[cls_ind][im_ind] = dets[keep, :].copy()
    return nms_boxes

def sentence(vocab, vocab_indices):
    # consider <eos> tag with id 0 in vocabulary
    sentence = ' '.join([vocab[i] for i in vocab_indices])
    suffix = ' ' + vocab[0]
    if sentence.endswith(suffix):
      sentence = sentence[:-len(suffix)]
    return sentence

def test_net(feature_net, embed_net, recurrent_net, offset_net, imdb, max_per_image=100, vis=True):
    """Test a Fast R-CNN network on an image database."""
    num_images = len(imdb.image_index)
    if DEBUG:
        print 'number of images: %d' % num_images
    # all detections are collected into:
    #    all_regions[image] = list of {'image_id', caption', 'location', 'location_seq'}
    all_regions = [None] * num_images
    results = {}
    output_dir = get_output_dir(imdb, feature_net)

    # timers
    _t = {'im_detect' : Timer(), 'misc' : Timer()}

    if not cfg.TEST.HAS_RPN:
        roidb = imdb.roidb

    #read vocabulary  & add <eos> tag
    vocab = list(imdb.get_vocabulary())
    vocab.insert(0, '<EOS>')

    for i in xrange(num_images):
        # filter out any ground truth boxes
        if cfg.TEST.HAS_RPN:
            box_proposals = None
        else:
            # The roidb may contain ground-truth rois (for example, if the roidb
            # comes from the training or val split). We only want to evaluate
            # detection on the *non*-ground-truth rois. We select those the rois
            # that have the gt_classes field set to 0, which means there's no
            # ground truth.
            box_proposals = roidb[i]['boxes'][roidb[i]['gt_classes'] == 0]

        im = cv2.imread(imdb.image_path_at(i))
        _t['im_detect'].tic()
        scores, boxes, captions = im_detect(feature_net, embed_net, recurrent_net, \
            offset_net, im, box_proposals)
        #features = extract_feature(net, im)

        _t['im_detect'].toc()

        _t['misc'].tic()
        # only one positive class
        if DEBUG:
            print 'shape of scores'
            print scores.shape
        
       
  
        pos_dets = np.hstack((boxes, scores[:,np.newaxis])) \
            .astype(np.float32, copy=False)
        keep = nms(pos_dets, cfg.TEST.NMS)
        pos_dets = pos_dets[keep, :]
        pos_scores = scores[keep]
        pos_captions = [sentence(vocab, captions[idx]) for idx in keep]
        pos_boxes = boxes[keep,:]
        if vis:
            #TODO(Linjie): display location sequence
            vis_detections(imdb.image_path_at(i), im, pos_captions, pos_dets, save_path = os.path.join(output_dir,'vis'))
        all_regions[i] = []
        #follow the format of baseline models routine
        for cap, box, prob in zip(pos_captions, pos_boxes, pos_scores):
            anno = {'image_id':i, 'prob': format(prob,'.3f'), 'caption':cap, \
            'location': box.tolist()}
            all_regions[i].append(anno)
        key = imdb.image_path_at(i).split('/')[-1]
        results[key] = {}
        results[key]['boxes'] = pos_boxes.tolist()
        results[key]['logprobs'] = np.log(pos_scores + eps).tolist()
        results[key]['captions'] = pos_captions
        

        
        _t['misc'].toc()

        print 'im_detect: {:d}/{:d} {:.3f}s {:.3f}s' \
              .format(i + 1, num_images, _t['im_detect'].average_time,
                      _t['misc'].average_time)
    # write file for evaluation with Torch code from Justin
    print 'write to result.json'
    det_file = os.path.join(output_dir, 'results.json')
    with open(det_file, 'w') as f:
        json.dump(results, f)

    print 'Evaluating detections'
    #imdb.evaluate_detections(all_regions, output_dir)
   
   
    
    #gt_regions = imdb.get_gt_regions() # is a list
    gt_regions_merged = [None] * num_images
    #transform gt_regions into the baseline model routine
    #for i,regions in enumerate(gt_regions):
    for i, image_index in enumerate(imdb.image_index):
        new_gt_regions = []
        regions = imdb.get_gt_regions_index(image_index)
        for reg in regions['regions']:
            loc = np.array([reg['x'], reg['y'], reg['x'] + reg['width'], reg['y'] + reg['height']])
            anno = {'image_id':i, 'caption': reg['phrase'].encode('ascii','ignore'), 'location': loc}
            new_gt_regions.append(anno)
        #merge regions with large overlapped areas
        assert(len(new_gt_regions) > 0)
        gt_regions_merged[i] = gt_region_merge(new_gt_regions)
    image_ids = range(num_images)
    vg_evaluator = VgEvalCap(gt_regions_merged, all_regions)
    vg_evaluator.params['image_id'] = image_ids
    vg_evaluator.evaluate()
    