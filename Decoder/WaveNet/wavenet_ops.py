import tensorflow as tf


'''
below is the vanilla wavenet, used for training vqvae decoder (or wavenet itself)
'''


def shift_right(x):
    shape = x.get_shape().as_list()
    x_padded = tf.pad(x, [[0, 0], [1, 0], [0, 0]])
    x_sliced = x_padded[:, :-1, :]
    x_sliced.set_shape(shape)
    return x_sliced


def mul_or_none(a, b):
    if a is None or b is None:
        return None
    return a * b


def time_to_batch(x, block_size):
    shape1 = x.get_shape().as_list()
    shape = tf.shape(x)
    y = tf.reshape(x, [shape[0], shape[1] // block_size, block_size, shape[2]])
    y = tf.transpose(y, [0, 2, 1, 3])
    y = tf.reshape(y, [shape[0] * block_size, shape[1] // block_size, shape[2]])
    y.set_shape([
        mul_or_none(shape1[0], block_size), mul_or_none(shape1[1], 1. / block_size),
        shape1[2]])
    return y


def batch_to_time(x, block_size):
    shape1 = x.get_shape().as_list()
    shape = tf.shape(x)
    y = tf.reshape(x, [shape[0] // block_size, block_size, shape[1], shape[2]])
    y = tf.transpose(y, [0, 2, 1, 3])
    y = tf.reshape(y, [shape[0] // block_size, shape[1] * block_size, shape[2]])
    y.set_shape([mul_or_none(shape1[0], 1. / block_size),
        mul_or_none(shape1[1], block_size),
        shape1[2]])
    return y


def causal_conv1d(net, weights, stride=1, padding='CAUSAL', dilations=1):
    net = time_to_batch(net, dilations)
    filter_length = weights['kernel'].shape.as_list()[0]
    if filter_length > 1 and padding == 'CAUSAL':
        net = tf.pad(net, [[0, 0], [filter_length - 1, 0], [0, 0]])
    padding = 'VALID' if padding == 'CAUSAL' else 'SAME'
    net = tf.nn.conv1d(net, weights['kernel'], stride=1, padding=padding)
    net += weights['bias']
    net = batch_to_time(net, dilations)
    return net


def conv1d_v2(net, filters, kernel_size, padding='CAUSAL', dilations=1):
    padding = padding.upper()
    if padding == 'CAUSAL':
        padding = 'VALID'

    in_channels = net.shape.as_list()[-1]
    kernel = tf.get_variable(name='kernel', 
                             shape=[kernel_size, in_channels, filters], 
                             dtype=tf.float32,
                             initializer=tf.uniform_unit_scaling_initializer(1.0),
                             regularizer=None)
    bias = tf.get_variable(name='bias', 
                           shape=[filters], 
                           dtype=tf.float32,
                           initializer=tf.constant_initializer(0.0),
                           regularizer=None)

    # in TF r1.14, arg 'dilations' is added to tf.nn.conv1d
    net = tf.pad(net, [[0, 0], [dilations * (kernel_size - 1), 0], [0, 0]])
    kernel = tf.expand_dims(kernel, axis=0) # [fw, in, out] -> [fh=1, fw, in, out]
    stride = [1, 1, 1, 1]
    dilations = [1, 1, dilations, 1]
    net = tf.expand_dims(net, axis=1) # [b, t, c] -> [b, h=1, t, c]
    net = tf.nn.conv2d(net, kernel, strides=stride, padding=padding, dilations=dilations)
    net = tf.squeeze(net, axis=1) + bias
    return net


def add_condition(net, condition):
    if condition is not None:
        _, net_len, out_channels = net.shape.as_list()
        T = condition.shape.as_list()[1]
        encoding = conv1d_v2(condition, out_channels, kernel_size=1)
        net = tf.reshape(net, [-1, T, net_len // T, out_channels])
        net += tf.expand_dims(encoding, 2)
        net = tf.reshape(net, [-1, net_len, out_channels])
    return net


def gated_cnn(net, dilation_filters, kernel_size, dilations, 
    local_condition, global_condition):
    net = conv1d_v2(net, 2 * dilation_filters, kernel_size, 'CAUSAL', dilations)
    with tf.variable_scope('local_condition'):
        net = add_condition(net, local_condition)
    with tf.variable_scope('global_condition'):
        net = add_condition(net, global_condition)

    net_filter, net_gate = net[:, :, : dilation_filters], net[:, :, dilation_filters: ]
    net = tf.nn.tanh(net_filter) * tf.nn.sigmoid(net_gate)
    return net


def residual_stack(net, dilation_filters, kernel_size, dilations, 
    skip_filters, residual_filters, local_condition, global_condition=None):
    ''' Performs one layer of residual stack, with skip and residual connections
    Args:
        net: tensor of shape [b, t, c]
        dilations: dilation rate
        *filters: out channels for that conv op
        local_condition: upsampled output from VQ-VAE, same resolution as net
    Returns:
        skip_connection, residual_connection
    '''
    with tf.variable_scope('gated'):
        gated = gated_cnn(net, dilation_filters, kernel_size, dilations, \
            local_condition, global_condition)

    with tf.variable_scope('skip'):
        skip_connection = conv1d_v2(gated, skip_filters, kernel_size=1)

    with tf.variable_scope('residual'):
        residual_connection = conv1d_v2(gated, residual_filters, kernel_size=1)

    return skip_connection, residual_connection


'''
below is the fast wavenet autoregressive generation, based on:
https://arxiv.org/abs/1611.09482
https://github.com/ibab/tensorflow-wavenet
'''

def linear(net, filters):
    ''' Performs one stride of a 1x1 convolution
    Args: 
        net: the current state
        filters: number of filters of result
    Returns:
        tensor of shape [b, filters]
    '''
    in_channels = net.shape.as_list()[-1]
    kernel = tf.get_variable(name='kernel', shape=[1, in_channels, filters])
    bias = tf.get_variable(name='bias', shape=[filters])
    return tf.matmul(net, kernel[0]) + bias


def fast_conv1d(past, current, filters, kernel_size=2):
    ''' performs one stride of convolution on a layer
    Args:
        past: the past state
        current: the current state
    Returns:
        a new state at t+1
    '''
    in_channels = current.shape.as_list()[-1]
    kernel_shape = [kernel_size, in_channels, filters]
    kernel = tf.get_variable(name='kernel', shape=kernel_shape)
    bias = tf.get_variable(name='bias', shape=[filters])

    kernel_past = kernel[0, :, :]
    kernel_current = kernel[1, :, :]
    return tf.matmul(past, kernel_past) + tf.matmul(current, kernel_current) + bias


def fast_condition(net, condition_t):
    ''' Adds condition to net. Global condition is concatenated to local condition.
    Args:
        net: the current state at time t
        condition_t: local condition at time t
    Returns:
        net added with 1x1 condition_t
    '''
    if condition_t is not None:
        out_channels = net.shape.as_list()[-1]
        net += linear(condition_t, out_channels)
    return net


def fast_gated_cnn(past, current, dilation_filters, kernel_size, 
    local_condition_t, global_condition_t):
    ''' Performs one stride of the gated convolution. 
    Args:
        past: the past state dequeued from q
        current: the current state at time t
        dilation_filters: number of filters for dilated_causal_conv
        kernel_size: filter width of dilated_causal_conv
        local_condition_t: local condition at time t, placeholder
        global_condition_t: local condition at time t, placeholder
    Returns:
        gated conv on net
    '''
    net = fast_conv1d(past, current, 2 * dilation_filters, kernel_size)
    with tf.variable_scope('local_condition'):
        net = fast_condition(net, local_condition_t)
    with tf.variable_scope('global_condition'):
        net = fast_condition(net, global_condition_t)
    # half of gated to tanh, half of gated to sigmoid
    net_filter, net_gate = net[:, : dilation_filters], net[:, dilation_filters: ]
    net = tf.nn.tanh(net_filter) * tf.nn.sigmoid(net_gate)
    return net


def fast_residual_stack(past, current, dilation_filters, kernel_size, 
    local_condition_t, global_condition_t, skip_filters, residual_filters):
    ''' Performs one stride of one layer of residual stack
    Args:
        past...condition_t: see fast_gated_cnn
        skip_filters: number of filters for skip_connection
        residual_filters: number of filters for residual_connection
    Returns:
        a tuple of skip connection of shape [b, skip_filters] and 
        residual_connection of shape [b, residual_filters]
    '''
    with tf.variable_scope('gated'):
        gated = fast_gated_cnn(past, current, dilation_filters, kernel_size, local_condition_t, global_condition_t)

    with tf.variable_scope('skip'):
        skip_connection = linear(gated, skip_filters)

    with tf.variable_scope('residual'):
        residual_connection = linear(gated, residual_filters)

    return skip_connection, residual_connection


