# Copyright 2017 The TensorFlow Authors All Rights Reserved.
# Copyright 2018 Xu Chen All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================

"""Library for capsule layers."""

from __future__ import absolute_import
from __future__ import division 
from __future__ import print_function

import numpy as np 
import tensorflow as tf 

from models.layers import variables

def _squash(in_tensor):
    """Applies (squash) to capsule layer.
    
    Args:
        in_tensor: tensor, 
            shape [batch, num_cap_types, num_atoms] for a fc capsule layer or
            shape [batch, num_cap_types, num_atoms, h, w] for a convolutional 
            capsule layer.
    Returns:
        A tensor with same shape
    """
    with tf.name_scope('norm_non_linearity'):
        norm = tf.norm(in_tensor, axis=2, keep_dims=True)
        norm_squared = norm * norm
        return (in_tensor / norm) * (norm_squared / (1 + norm_squared))

def _leaky_routing(logits, out_dim):
    """Adds extra dimmension to routing logits.

    This enables active capsules to be routed to the extra dim if they are not a
    good fit for any of the capsules in the layer above.

    Args:
        logits: the original logits. shape (in_dim, out_dim) if fully connected. 
            Otherwise, it has two more dimmensions.
        out_dim:
    
    Returns:
        routing probabilities for each pair of capsules. Same shape as logits.
    """
    leak = tf.zeros_like(logits, optimize=True)
    leak = tf.reduce_sum(leak, axis=2, keep_dims=True)
    leaky_logits = tf.concat([leak, logits], axis=2)
    leaky_routing = tf.nn.softmax(leaky_logits, dim=2)
    return tf.split(leaky_routing, [1, out_dim], 2)[1]

def _update_routing(votes, biases, logit_shape, num_ranks, in_dim, out_dim,
                    leaky, num_routing):
    """Sums over scaled votes and applies squash to compute the activations.

    Iteratively updates routing logits (scales) based on the similarity between
    the activation of this layer and the votes of the layer below.

    Args:
        votes: tensor, the transformed outputs of the layer below.
        biases: tensor, bias variable.
        logit_shape: tensor, shape of the logit to be initialized.
        num_ranks: scalar, rank of the votes tensor. For fully connected capsule it
            is 4, for convolutional capsule it is 6.
        in_dim: scalar, number of capsule types of input.
        out_dim: scalar, number of capsule types of output.
        leaky: boolean, whether to use leaky routing.
        num_routing: scalar, number of routing iterations.
    Returns:
        The activation tensor of the output layer after `num_routing` iterations.
    """
    votes_t_shape = [3, 0, 1, 2]
    r_t_shape = [1, 2, 3, 0]
    for i in range(num_ranks - 4):
        votes_t_shape += [i + 4]
        r_t_shape += [i + 4]
    votes_trans = tf.transpose(votes, votes_t_shape)

    def _body(i, logits, activations):
        """Routing while loop."""
        # route: [batch, in_dim, out_dim, ...]
        if leaky:
            route = _leaky_routing(logits, out_dim)
        else:
            route = tf.nn.softmax(logits, dim=2)
        preact_unrolled = route * votes_trans
        preact_trans = tf.transpose(preact_unrolled, r_t_shape)
        preactivate = tf.reduce_sum(preact_trans, axis=1) + biases
        activation = _squash(preactivate)
        activations = activations.write(i, activation)
        act_3d = tf.expand_dims(activation, 1)
        tile_shape = np.ones(num_ranks, dtype=np.int32).tolist()
        tile_shape[1] = in_dim
        act_replicated = tf.tile(act_3d, tile_shape)
        distances = tf.reduce_sum(votes * act_replicated, axis=3)
        logits += distances
        return (i + 1, logits, activations)

    activations = tf.TensorArray(
        dtype=tf.float32, size=num_routing, clear_after_read=False)
    logits = tf.fill(logit_shape, 0.0)
    i = tf.constant(0, dtype=tf.int32)
    _, logits, activations = tf.while_loop(
        lambda i, logits, activations: i < num_routing,
        _body, 
        loop_vars=[i, logits, activations],
        swap_memory=True)
    """visual"""
    for i in range(num_routing):
        tf.add_to_collection('visual', activations.read(i))

    return activations.read(num_routing - 1)
    

def _depthwise_conv3d(in_tensor, in_dim, in_atoms,
                      out_dim, out_atoms,
                      kernel, stride=2, padding='SAME'):
    """Perform 2D convolution given a 5D input tensor.

    This layer given an input tensor of shape (batch, in_dim, in_atoms, in_h, in_w).
    We squeeze this first two dimmensions to get a 4R tensor as the input of 
    tf.nn.conv2d. Then splits the first dimmension and the last dimmension and 
    returns the 6R convolution output.

    Args:
        in_tensor: 5R tensor, last two dimmensions representing height and width.
        in_dim: scalar, number of capsule types of input.
        in_atoms: scalar, number of units of each input capsule.
        out_dim: scalar, number of capsule types of output.
        out_atoms: scalar, number of units of each output capsule.
        kernel: tensor, convolutional kernel variable.
        stride: scalar, stride of the convolutional kernel.
        padding: 'SAME' or 'VALID', padding mechanism for convolutional kernels.
    Returns: 
        6R tensor output of a 2D convolution with shape (batch, in_dim, out_dim,
        out_atoms, out_h, out_w), the covolution output shape and the input shape.
    """
    with tf.name_scope('conv'):
        in_shape = tf.shape(in_tensor) # op
        _, _, _, in_height, in_width = in_tensor.get_shape() # (batch, in_dim, in_atoms, in_h, in_w)
        # Reshape in_tensor to 4R by merging first two dimmensions.
        in_tensor_reshaped = tf.reshape(in_tensor, [
            in_shape[0]*in_dim, in_atoms, in_shape[3], in_shape[4]
        ])
        in_tensor_reshaped.set_shape((None, in_atoms, in_height.value, in_width.value))
        
        # do convolution
        conv = tf.nn.conv2d(
            in_tensor_reshaped,
            kernel, [1, 1, stride, stride],
            padding=padding,
            data_format='NCHW')
        conv_shape = tf.shape(conv) # shape (batch*in_dim, out_dim*out_atoms, H, W)
        _, _, conv_height, conv_width = conv.get_shape() 
        # Reshape back to 6R by splitting first dimmension to batch and in_dim
        # and splitting the second dimmension to out_dim and out_atoms.
        
        conv_reshaped = tf.reshape(conv, [
            in_shape[0], in_dim, out_dim, out_atoms, conv_shape[2], conv_shape[3]
        ], name='votes')
        conv_reshaped.set_shape((None, in_dim, out_dim, out_atoms, 
            conv_height.value, conv_width.value))

        """visual"""
        tf.add_to_collection('visual', conv_reshaped)
        return conv_reshaped, conv_shape, in_shape
        
def conv_slim_capsule(in_tensor, in_dim, in_atoms,
                      out_dim, out_atoms, layer_name,
                      kernel_size=5, stride=2, padding='SAME', **routing_args):
    """Builds a slim convolutional capsule layer.

    This layer performs 2D convolution given 5R input tensor of shape
    (batch, in_dim, in_atoms, in_h, in_w). Then refines the votes with 
    routing and applies Squash nonlinearity for each capsule.

    Each capsule in this layer is a convolutional unit and shares its kernel
    over its positional grid (e.g. 9x9) and different capsules below. Therefore,
    number of trainable variables in this layer is:

        kernel: (kernel_size, kernel_size, in_atoms, out_dim * out_atoms)
        bias: (out_dim, out_atoms)
    
    Output of a conv2d layer is a single capsule with channel number of atoms.
    Therefore conv_slim_capsule is suitable to be added on top of a conv2d layer
    with num_routing=1, in_dim=1 and in_atoms = conv_channels.

    Args:
        in_tensor: 5R tensor, last two dimmensions representing height and width.
        in_dim: scalar, number of capsule types of input.
        in_atoms: scalar, number of units of each input capsule.
        out_dim: scalar, number of capsule types of output.
        out_atoms: scalar, number of units of each output capsule.
        layer_name: string, name of this layer.
        kernel_size: scalar: convolutional kernel size (kernel_size, kernel_size)
        stride: scalar, stride of the convolutional kernel.
        padding: 'SAME' or 'VALID', padding mechanism for convolutional kernels.
        **routing_args: dictionary {leaky, num_routing}, args to be passed to the 
            routing procedure.
    Returns:
        Tensor of activations for this layer of shape
            (batch, out_dim, out_atoms, out_h, out_w).
    """
    with tf.variable_scope(layer_name):
        kernel = variables.weight_variable(
            shape=[kernel_size, kernel_size, in_atoms, out_dim * out_atoms])
        biases = variables.bias_variable(
            shape=[out_dim, out_atoms, 1, 1]) 
        votes, votes_shape, in_shape = _depthwise_conv3d(
            in_tensor, in_dim, in_atoms, out_dim, out_atoms, kernel, stride, padding)
        
        with tf.name_scope('routing'):
            logit_shape = tf.stack([
                in_shape[0], in_dim, out_dim, votes_shape[2], votes_shape[3]
            ])
            biases_replicated = tf.tile(biases, [1, 1, votes_shape[2], votes_shape[3]])

            activations = _update_routing(
                votes=votes, 
                biases=biases_replicated, 
                logit_shape=logit_shape, 
                num_ranks=6, 
                in_dim=in_dim, 
                out_dim=out_dim,
                **routing_args)
        return activations

def capsule(in_tensor, in_dim, in_atoms,
            out_dim, out_atoms, layer_name,
            **routing_args):
    """Builds a fully connected capsule layer.

    Given an input tensor of shape (batch, in_dim, in_atoms), this op
    performs the following:

        1. For each input capsule, multiplies it with the weight variables
        to get votes of shape (batch, in_dim, out_dim, out_atoms);
        2. Scales the votes for each output capsule by routing;
        3. Squashes the output of each capsule to have norm less than one.

    Each capsule of this layer has one weight tensor for each capsule of
    layer below. Therefore, this layer has the following number of 
    trainable variables:
        kernel: (in_dim, in_atoms, out_dim * out_atoms)
        biases: (out_dim, out_atoms)
    
    Args:
        in_tensor: tensor, activation output of the layer below.
        in_dim: scalar, number of capsule types in the layer below.
        in_atoms: scalar, number of units of input capsule.
        out_dim: scalar, number of capsule types in the output layer.
        out_atoms: scalar, number of units of output capsule.
        layer_name: string, the number of this layer.
        **routing_args: dictionary {leaky, num_routing}, args for routing.
    Returns:
        Tensor of activations for this layer of shape (batch, out_dim, out_atoms).
    """
    with tf.variable_scope(layer_name):
        weights = variables.weight_variable([
            in_dim, in_atoms, out_dim * out_atoms
        ])
        biases = variables.bias_variable([out_dim, out_atoms])
        with tf.name_scope('Wx_plus_b'):
            # Depthwise matmul: [b, d, c] @ [d, c, o_c] = [b, d, o_c]
            # to do this: tile input, do element-wise multiplication and reduce
            # sum over in_atoms dimmension.
            in_tiled = tf.tile(
                tf.expand_dims(in_tensor, -1), 
                [1, 1, 1, out_dim * out_atoms])
            votes = tf.reduce_sum(in_tiled * weights, axis=2)
            votes_reshaped = tf.reshape(votes,
                                        [-1, in_dim, out_dim, out_atoms])
        
        with tf.name_scope('routing'):
            in_shape = tf.shape(in_tensor)
            logit_shape = tf.stack([in_shape[0], in_dim, out_dim])
            activations = _update_routing(
                votes=votes_reshaped,
                biases=biases, 
                logit_shape=logit_shape, 
                num_ranks=1, 
                in_dim=in_dim, 
                out_dim=out_dim,
                **routing_args)
        
        return activations
    