
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd import Variable


from .proposal_layer import _ProposalLayer
from .anchor_target_layer import _AnchorTargetLayer
from utils.functions import _smooth_l1_loss


class RPN(nn.Module):
    """ region proposal network """

    def __init__(self, config, din, anchor_scales, anchor_ratios, feat_stride):

        super(RPN, self).__init__()

        self.din = din  # get depth of input feature map, e.g., 512
        self.anchor_scales = anchor_scales
        self.anchor_ratios = anchor_ratios
        self.feat_stride = feat_stride[0]

        # define the convrelu layers processing input feature map
        self.RPN_Conv = nn.Conv2d(self.din, 512, 3, 1, 1, bias=True)

        # define bg/fg classifcation score layer
        self.nc_score_out = len(self.anchor_scales) * len(self.anchor_ratios) * 2  # 2(bg/fg) * 9 (anchors)
        self.RPN_cls_score = nn.Conv2d(512, self.nc_score_out, 1, 1, 0)

        # define anchor box offset prediction layer
        self.nc_bbox_out = len(self.anchor_scales) * len(self.anchor_ratios) * 4  # 4(coords) * 9 (anchors)
        self.RPN_bbox_pred = nn.Conv2d(512, self.nc_bbox_out, 1, 1, 0)

        # define proposal layer
        self.RPN_proposal = _ProposalLayer(feat_stride=self.feat_stride,
                                           scales=self.anchor_scales,
                                           ratios=self.anchor_ratios,
                                           pre_nms_top_n={'TRAIN': config.train.rpn.pre_nms_top_n,
                                                          'TEST': config.test.rpn.pre_nms_top_n},
                                           post_nms_top_n={'TRAIN': config.train.rpn.post_nms_top_n,
                                                           'TEST': config.test.rpn.post_nms_top_n},
                                           nms_thresh={'TRAIN': config.train.rpn.nms_thresh,
                                                       'TEST': config.test.rpn.nms_thresh},
                                           min_size={'TRAIN': config.train.rpn.min_size,
                                                     'TEST': config.test.rpn.min_size})

        # define anchor target layer
        self.RPN_anchor_target = _AnchorTargetLayer(feat_stride=self.feat_stride,
                                                    scales=self.anchor_scales,
                                                    ratios=self.anchor_ratios,
                                                    rpn_batch_size=config.train.rpn.batch_size,
                                                    clobber_positives=config.train.rpn.clobber_positives,
                                                    fg_fraction=config.train.rpn.fg_fraction,
                                                    positive_overlap=config.train.rpn.positive_overlap,
                                                    negative_overlap=config.train.rpn.negative_overlap,
                                                    positive_weight=config.train.rpn.positive_weight,
                                                    bbox_inside_weights=config.train.rpn.bbox_inside_weights)

        self.rpn_loss_cls = 0
        self.rpn_loss_box = 0

    @staticmethod
    def reshape(x, d):
        input_shape = x.size()
        x = x.view(
            input_shape[0],
            int(d),
            int(float(input_shape[1] * input_shape[2]) / float(d)),
            input_shape[3]
        )
        return x

    def forward(self, base_feat, im_info, gt_boxes, num_boxes):
        batch_size = base_feat.size(0)

        # return feature map after convrelu layer
        rpn_conv1 = F.relu(self.RPN_Conv(base_feat), inplace=True)
        # get rpn classification score
        rpn_cls_score = self.RPN_cls_score(rpn_conv1)

        rpn_cls_score_reshape = self.reshape(rpn_cls_score, 2)
        rpn_cls_prob_reshape = F.softmax(rpn_cls_score_reshape, 1)
        rpn_cls_prob = self.reshape(rpn_cls_prob_reshape, self.nc_score_out)

        # get rpn offsets to the anchor boxes
        rpn_bbox_pred = self.RPN_bbox_pred(rpn_conv1)

        # proposal layer
        cfg_key = 'TRAIN' if self.training else 'TEST'

        rois = self.RPN_proposal((rpn_cls_prob.data, rpn_bbox_pred.data, im_info, cfg_key))

        self.rpn_loss_cls = 0
        self.rpn_loss_box = 0

        # generating training labels and build the rpn loss
        if self.training:
            assert gt_boxes is not None

            rpn_data = self.RPN_anchor_target((rpn_cls_score.data, gt_boxes, im_info, num_boxes))

            # compute classification loss
            rpn_cls_score = rpn_cls_score_reshape.permute(0, 2, 3, 1).contiguous().view(batch_size, -1, 2)
            rpn_label = rpn_data[0].view(batch_size, -1)

            rpn_keep = Variable(rpn_label.view(-1).ne(-1).nonzero().view(-1))
            rpn_cls_score = torch.index_select(rpn_cls_score.view(-1, 2), 0, rpn_keep)
            rpn_label = torch.index_select(rpn_label.view(-1), 0, rpn_keep.data)
            rpn_label = Variable(rpn_label.long())
            self.rpn_loss_cls = F.cross_entropy(rpn_cls_score, rpn_label)
            fg_cnt = torch.sum(rpn_label.data.ne(0))

            rpn_bbox_targets, rpn_bbox_inside_weights, rpn_bbox_outside_weights = rpn_data[1:]

            # compute bbox regression loss
            rpn_bbox_inside_weights = Variable(rpn_bbox_inside_weights)
            rpn_bbox_outside_weights = Variable(rpn_bbox_outside_weights)
            rpn_bbox_targets = Variable(rpn_bbox_targets)

            self.rpn_loss_box = _smooth_l1_loss(rpn_bbox_pred, rpn_bbox_targets, rpn_bbox_inside_weights,
                                                rpn_bbox_outside_weights, sigma=3, dim=[1, 2, 3])

        return rois, self.rpn_loss_cls, self.rpn_loss_box