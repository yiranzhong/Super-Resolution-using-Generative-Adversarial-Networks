from keras import backend as K
from keras.models import Model
from keras.layers import Input, merge, BatchNormalization, LeakyReLU, Flatten, Dense, Lambda
from keras.layers.convolutional import Convolution2D, MaxPooling2D
from keras.optimizers import Adam
from keras.preprocessing.image import ImageDataGenerator
from keras.utils.data_utils import get_file

from keras_training_ops import fit as bypass_fit

from layers import Normalize, Denormalize, depth_to_scale
from loss import AdversarialLossRegularizer, ContentVGGRegularizer, TVRegularizer, psnr, dummy_loss

import os
import time
import h5py
import numpy as np
import json
from scipy.misc import imresize, imsave
from scipy.ndimage.filters import gaussian_filter

THEANO_WEIGHTS_PATH_NO_TOP = r'https://github.com/fchollet/deep-learning-models/releases/download/v0.1/vgg16_weights_th_dim_ordering_th_kernels_notop.h5'

if not os.path.exists("weights/"):
    os.makedirs("weights/")

if not os.path.exists("val_images/"):
    os.makedirs("val_images/")

class VGGNetwork:
    '''
    Helper class to load VGG and its weights to the FastNet model
    '''

    def __init__(self, img_width=384, img_height=384, vgg_weight=1.0):
        self.img_height = img_height
        self.img_width = img_width
        self.vgg_weight = vgg_weight

        self.vgg_layers = None

    def append_vgg_network(self, x_in, true_X_input, pre_train=False):

        # Append the initial inputs to the outputs of the SRResNet
        x = merge([x_in, true_X_input], mode='concat', concat_axis=0)

        # Normalize the inputs via custom VGG Normalization layer
        x = Normalize(name="normalize_vgg")(x)


        # Begin adding the VGG layers
        x = Convolution2D(64, 3, 3, activation='relu', name='vgg_conv1_1', border_mode='same')(x)

        x = Convolution2D(64, 3, 3, activation='relu', name='vgg_conv1_2', border_mode='same')(x)
        x = MaxPooling2D(name='vgg_maxpool1')(x)

        x = Convolution2D(128, 3, 3, activation='relu', name='vgg_conv2_1', border_mode='same')(x)

        if pre_train:
            vgg_regularizer2 = ContentVGGRegularizer(weight=self.vgg_weight / 4)
            x = Convolution2D(128, 3, 3, activation='relu', name='vgg_conv2_2', border_mode='same',
                              activity_regularizer=vgg_regularizer2)(x)
        else:
            x = Convolution2D(128, 3, 3, activation='relu', name='vgg_conv2_2', border_mode='same')(x)
        x = MaxPooling2D(name='vgg_maxpool2')(x)

        x = Convolution2D(256, 3, 3, activation='relu', name='vgg_conv3_1', border_mode='same')(x)
        x = Convolution2D(256, 3, 3, activation='relu', name='vgg_conv3_2', border_mode='same')(x)

        x = Convolution2D(256, 3, 3, activation='relu', name='vgg_conv3_3', border_mode='same')(x)
        x = MaxPooling2D(name='vgg_maxpool3')(x)

        x = Convolution2D(512, 3, 3, activation='relu', name='vgg_conv4_1', border_mode='same')(x)
        x = Convolution2D(512, 3, 3, activation='relu', name='vgg_conv4_2', border_mode='same')(x)

        x = Convolution2D(512, 3, 3, activation='relu', name='vgg_conv4_3', border_mode='same')(x)
        x = MaxPooling2D(name='vgg_maxpool4')(x)

        x = Convolution2D(512, 3, 3, activation='relu', name='vgg_conv5_1', border_mode='same')(x)
        x = Convolution2D(512, 3, 3, activation='relu', name='vgg_conv5_2', border_mode='same')(x)

        if not pre_train:
            vgg_regularizer5 = ContentVGGRegularizer(weight=self.vgg_weight)
            x = Convolution2D(512, 3, 3, activation='relu', name='vgg_conv5_3', border_mode='same',
                          activity_regularizer=vgg_regularizer5)(x)
        else:
            x = Convolution2D(512, 3, 3, activation='relu', name='vgg_conv5_3', border_mode='same')(x)
        x = MaxPooling2D(name='vgg_maxpool5')(x)

        return x

    def load_vgg_weight(self, model):
        # Loading VGG 16 weights
        weights_name = "vgg16_weights_th_dim_ordering_th_kernels_notop.h5"
        weights_path = THEANO_WEIGHTS_PATH_NO_TOP

        weights = get_file(weights_name, weights_path, cache_subdir='models')
        f = h5py.File(weights)

        layer_names = [name for name in f.attrs['layer_names']]

        if self.vgg_layers is None:
            self.vgg_layers = [layer for layer in model.layers
                               if 'vgg_' in layer.name]

        for i, layer in enumerate(self.vgg_layers):
            g = f[layer_names[i]]
            weights = [g[name] for name in g.attrs['weight_names']]
            layer.set_weights(weights)
        print('VGG Model weights loaded.')

        # Freeze all VGG layers
        for layer in self.vgg_layers:
            layer.trainable = False

        return model


class DiscriminatorNetwork:

    def __init__(self, img_width=384, img_height=384, adversarial_loss_weight=1e-3, small_model=False):
        self.img_width = img_width
        self.img_height = img_height
        self.adversarial_loss_weight = adversarial_loss_weight
        self.small_model = small_model

        self.k = 3
        self.mode = 2
        self.weights_path = "weights/Discriminator weights.h5"

        self.gan_layers = None

    def append_gan_network(self, true_X_input):

        # Normalize the inputs via custom VGG Normalization layer
        x = Normalize(type="gan", value=255., name="gan_normalize")(true_X_input)

        x = Convolution2D(64, self.k, self.k, border_mode='same', name='gan_conv1_1')(x)
        x = LeakyReLU(0.3, name="gan_lrelu1_1")(x)

        x = Convolution2D(64, self.k, self.k, border_mode='same', name='gan_conv1_2', subsample=(2, 2))(x)
        x = LeakyReLU(0.3, name='gan_lrelu1_2')(x)
        x = BatchNormalization(mode=self.mode, axis=1, name='gan_batchnorm1_1')(x)

        filters = [128, 256] if self.small_model else [128, 256, 512]

        for i, nb_filters in enumerate(filters):
            for j in range(2):
                subsample = (2, 2) if j == 1 else (1, 1)

                x = Convolution2D(nb_filters, self.k, self.k, border_mode='same', subsample=subsample,
                                  name='gan_conv%d_%d' % (i + 2, j + 1))(x)
                x = LeakyReLU(0.3, name='gan_lrelu_%d_%d' % (i + 2, j + 1))(x)
                x = BatchNormalization(mode=self.mode, axis=1, name='gan_batchnorm%d_%d' % (i + 2, j + 1))(x)

        x = Flatten(name='gan_flatten')(x)

        output_dim = 128 if self.small_model else 1024

        x = Dense(output_dim, name='gan_dense1')(x)
        x = LeakyReLU(0.3, name='gan_lrelu5')(x)

        gan_regulrizer = AdversarialLossRegularizer(weight=self.adversarial_loss_weight)
        x = Dense(1, activation="sigmoid", activity_regularizer=gan_regulrizer, name='gan_output')(x)

        return x

    def set_trainable(self, model, value=True):
        if self.gan_layers is None:
            self.gan_layers = [layer for layer in model.layers
                      if 'gan_' in layer.name]

        for layer in self.gan_layers:
            layer.trainable = value

    def load_gan_weights(self, model):
        f = h5py.File(self.weights_path)

        layer_names = [name for name in f.attrs['layer_names']]
        layer_names = layer_names[3:] # First 2 are inputs, and 3rd is merge. Not needed.

        if self.gan_layers is None:
            self.gan_layers = [layer for layer in model.layers
                                if 'gan_' in layer.name]

        for i, layer in enumerate(self.gan_layers):
            g = f[layer_names[i]]
            weights = [g[name] for name in g.attrs['weight_names']]
            layer.set_weights(weights)

        print("GAN Model weights loaded.")
        return model

    def save_gan_weights(self, model):
        # true_x_input = Input(shape=(3, self.img_width, self.img_height))
        #
        # temp_gan_out = self.append_gan_network(true_x_input)
        # temp_gan_model = Model(true_x_input, temp_gan_out)
        #
        # if self.gan_layers is None:
        #     self.gan_layers = [layer for layer in model.layers
        #                         if 'gan_' in layer.name]
        #
        # temp_gan_layers = [layer for layer in temp_gan_model.layers]
        # temp_gan_layers = temp_gan_layers[1:] # First 2 are input layers, 3rd is merge layer. Not needed.
        #
        # len_gan = len(self.gan_layers)
        # len_temp_gan = len(temp_gan_layers)
        # assert len_gan == len_temp_gan, "Number of layers in temporary GAN layer does not " \
        #                                 "match the number of GAN layers in SRGAN model.\n" \
        #                                 "Number of layers in temp_gan = %d while " \
        #                                 "Number of layers in gan_layers = %d" % (len_gan, len_temp_gan)
        #
        # for i, temp_gan_layer in enumerate(temp_gan_layers):
        #     temp_gan_layer.set_weights(self.gan_layers[i].get_weights())

        print('GAN Weights are being saved.')
        model.save_weights(self.weights_path, overwrite=True)
        print('GAN Weights saved.')


class GenerativeNetwork:

    def __init__(self, img_width=96, img_height=96, batch_size=16, nb_upscales=2, small_model=False,
                 content_weight=1, gan_weight=5e2, tv_weight=2e-8, gen_channels=64):
        self.img_width = img_width
        self.img_height = img_height
        self.batch_size = batch_size
        self.small_model = small_model
        self.nb_scales = nb_upscales

        self.content_weight = content_weight
        self.gan_weight = gan_weight
        self.tv_weight = tv_weight

        self.filters = gen_channels
        self.mode = 2

        self.sr_res_layers = None
        self.sr_weights_path = "weights/SRGAN.h5"

        self.output_func = None

    def create_sr_model(self, ip):

        x = Convolution2D(self.filters, 5, 5, activation='linear', border_mode='same', name='sr_res_conv1')(ip)
        x = BatchNormalization(axis=1, mode=self.mode, name='sr_res_bn_1')(x)
        x = LeakyReLU(alpha=0.25, name='sr_res_lr1')(x)

        x = Convolution2D(self.filters, 5, 5, activation='linear', border_mode='same', name='sr_res_conv2')(x)
        x = BatchNormalization(axis=1, mode=self.mode, name='sr_res_bn_2')(x)
        x = LeakyReLU(alpha=0.25, name='sr_res_lr2')(x)

        nb_residual = 5 if self.small_model else 15

        for i in range(nb_residual):
            x = self._residual_block(x, i + 1)

        for scale in range(self.nb_scales):
            x = self._upscale_block(x, scale + 1)

        scale = 2 ** self.nb_scales
        tv_regularizer = TVRegularizer(img_width=self.img_width * scale, img_height=self.img_height * scale,
                                       weight=self.tv_weight)

        x = Convolution2D(3, 5, 5, activation='tanh', border_mode='same', activity_regularizer=tv_regularizer,
                          name='sr_res_conv_final')(x)

        x = Denormalize()(x)

        return x

    def _residual_block(self, ip, id):
        init = ip

        x = Convolution2D(self.filters, 3, 3, activation='linear', border_mode='same', name='sr_res_conv_' + str(id) + '_1')(ip)
        x = BatchNormalization(axis=1, mode=self.mode, name='sr_res_bn_' + str(id) + '_1')(x)
        x = LeakyReLU(alpha=0.25, name="sr_res_activation_" + str(id) + "_1")(x)

        x = Convolution2D(self.filters, 3, 3, activation='linear', border_mode='same', name='sr_res_conv_' + str(id) + '_2')(x)
        x = BatchNormalization(axis=1, mode=self.mode, name='sr_res_bn_' + str(id) + '_2')(x)
        x = LeakyReLU(alpha=1., name="sr_res_activation_" + str(id) + "_2")(x)

        m = merge([x, init], mode='sum', name="sr_res_merge_" + str(id))

        return m

    def _upscale_block(self, ip, id):
        init = ip
        scale = 2 ** id

        if K.image_dim_ordering() == "th":
            output_shape = (self.filters, self.img_width * scale, self.img_height * scale)
        else:
            output_shape = (self.img_width * scale, self.img_height * scale, self.filters)

        x = Convolution2D(256, 3, 3, activation="linear", border_mode='same', name='sr_res_upconv1_%d' % id)(init)
        x = LeakyReLU(alpha=0.25, name='sr_res_up_lr_%d_1_1' % id)(x)
        x = Lambda(lambda x: depth_to_scale(x, scale=2, channels=self.filters), output_shape=output_shape,
                   name='sr_res_upscale1_%d' % id)(x)
        x = Convolution2D(256, 3, 3, activation="linear", border_mode='same', name='sr_res_filter1_%d' % id)(x)
        x = LeakyReLU(alpha=0.25, name='sr_res_up_lr_%d_1_2' % id)(x)

        return x

    def set_trainable(self, model, value=True):
        if self.sr_res_layers is None:
            self.sr_res_layers = [layer for layer in model.layers
                                    if 'sr_res_' in layer.name]

        for layer in self.sr_res_layers:
            layer.trainable = value

    def get_generator_output(self, input_img, srgan_model):
        if self.output_func is None:
            gen_output_layer = [layer for layer in srgan_model.layers
                                if layer.name == "sr_res_conv_final"][0]
            self.output_func = K.function([srgan_model.layers[0].input],
                                          [gen_output_layer.output])

        return self.output_func([input_img])


class SRGANNetwork:

    def __init__(self, img_width=96, img_height=96, batch_size=16, nb_scales=2):
        self.img_width = img_width
        self.img_height = img_height
        self.batch_size = batch_size
        self.nb_scales = nb_scales

        self.discriminative_network = None # type: DiscriminatorNetwork
        self.generative_network = None # type: GenerativeNetwork
        self.vgg_network = None # type: VGGNetwork

        self.srgan_model_ = None # type: Model
        self.generative_model_ = None # type: Model
        self.discriminative_model_ = None #type: Model

    def build_srgan_model(self, use_small_srgan=False, use_small_discriminator=False):
        large_width = self.img_width * 4
        large_height = self.img_height * 4

        self.generative_network = GenerativeNetwork(self.img_width, self.img_height, self.batch_size, nb_upscales=self.nb_scales,
                                                    small_model=use_small_srgan)
        self.discriminative_network = DiscriminatorNetwork(large_width, large_height,
                                                           small_model=use_small_discriminator)
        self.vgg_network = VGGNetwork(large_width, large_height)

        ip = Input(shape=(3, self.img_width, self.img_height), name='x_generator')
        ip_gan = Input(shape=(3, large_width, large_height), name='x_discriminator') # Actual X images
        ip_vgg = Input(shape=(3, large_width, large_height), name='x_vgg') # Actual X images

        sr_output = self.generative_network.create_sr_model(ip)
        self.generative_model_ = Model(ip, sr_output)

        gan_output = self.discriminative_network.append_gan_network(ip_gan)
        self.discriminative_model_ = Model(ip_gan, gan_output)

        generator_output = self.generative_model_(ip)
        gan_output = self.discriminative_model_(generator_output)
        vgg_output = self.vgg_network.append_vgg_network(generator_output, ip_vgg)

        self.srgan_model_ = Model(input=[ip, ip_vgg],
                                  output=[gan_output, vgg_output])

        self.vgg_network.load_vgg_weight(self.srgan_model_)

        srgan_optimizer = Adam(lr=1e-4)

        self.generative_model_.compile(srgan_optimizer, dummy_loss)
        self.discriminative_model_.compile(srgan_optimizer, loss='binary_crossentropy', metrics=['acc'])
        self.srgan_model_.compile(srgan_optimizer, dummy_loss)

        return self.srgan_model_

    def build_discriminator_pretrain_model(self, use_small_srgan=False, use_small_discriminator=False):
        large_width = self.img_width * 4
        large_height = self.img_height * 4

        self.generative_network = GenerativeNetwork(self.img_width, self.img_height, self.batch_size, self.nb_scales,
                                                    use_small_srgan)
        self.discriminative_network = DiscriminatorNetwork(large_width, large_height,
                                                           small_model=use_small_discriminator)

        ip = Input(shape=(3, self.img_width, self.img_height), name='x_generator')
        ip_gan = Input(shape=(3, large_width, large_height), name='x_discriminator')  # Actual X images

        sr_output = self.generative_network.create_sr_model(ip)
        self.generative_model_ = Model(ip, sr_output)
        self.generative_network.set_trainable(self.generative_model_, value=False)

        gan_output = self.discriminative_network.append_gan_network(ip_gan)
        self.discriminative_model_ = Model(ip_gan, gan_output)

        generator_out = self.generative_model_(ip)
        gan_output = self.discriminative_model_(generator_out)

        self.srgan_model_ = Model(input=ip, output=gan_output)

        srgan_optimizer = Adam(lr=1e-4)

        self.generative_model_.compile(srgan_optimizer, loss='mse')
        self.discriminative_model_.compile(srgan_optimizer, loss='binary_crossentropy', metrics=['acc'])
        self.srgan_model_.compile(srgan_optimizer, loss='binary_crossentropy', metrics=['acc'])

        return self.discriminative_model_

    def build_srgan_pretrain_model(self, use_small_srgan=False):
        large_width = self.img_width * 4
        large_height = self.img_height * 4

        self.generative_network = GenerativeNetwork(self.img_width, self.img_height, self.batch_size, self.nb_scales,
                                                    use_small_srgan)
        self.vgg_network = VGGNetwork(large_width, large_height)

        ip = Input(shape=(3, self.img_width, self.img_height), name='x_generator')
        ip_vgg = Input(shape=(3, large_width, large_height), name='x_vgg')  # Actual X images

        sr_output = self.generative_network.create_sr_model(ip)
        self.generative_model_ = Model(ip, sr_output)

        vgg_output = self.vgg_network.append_vgg_network(sr_output, ip_vgg, pre_train=True)

        self.srgan_model_ = Model(input=[ip, ip_vgg],
                                  output=vgg_output)

        self.vgg_network.load_vgg_weight(self.srgan_model_)

        self.generative_network.set_trainable(self.srgan_model_, value=True)

        srgan_optimizer = Adam(lr=1e-4)
        self.generative_model_.compile(srgan_optimizer, dummy_loss)
        self.srgan_model_.compile(srgan_optimizer, dummy_loss)

        return self.srgan_model_

    def pre_train_srgan(self, image_dir, nb_images=50000, nb_epochs=1, use_small_srgan=False):
        self.build_srgan_pretrain_model(use_small_srgan=use_small_srgan)

        self._train_model(image_dir, nb_images=nb_images, nb_epochs=nb_epochs, pre_train_srgan=True)

    def pre_train_discriminator(self, image_dir, nb_images=50000, nb_epochs=1, use_small_discriminator=False):
        self.build_discriminator_pretrain_model(use_small_discriminator)

        self._train_model(image_dir, nb_images, nb_epochs, pre_train_discriminator=True,
                          load_generative_weights=True)

    def train_full_model(self, image_dir, nb_images=50000, nb_epochs=10, use_small_srgan=False,
                         use_small_discriminator=False):

        self.build_srgan_model(use_small_srgan, use_small_discriminator)

        self._train_model(image_dir, nb_images, nb_epochs, load_generative_weights=True, load_discriminator_weights=True)

    def _train_model(self, image_dir, nb_images=80000, nb_epochs=10, pre_train_srgan=False,
                     pre_train_discriminator=False, load_generative_weights=False, load_discriminator_weights=False,
                     save_loss=True):

        assert self.img_width >= 16, "Minimum image width must be at least 16"
        assert self.img_height >= 16, "Minimum image height must be at least 16"

        if load_generative_weights:
            self.generative_model_.load_weights(self.generative_network.sr_weights_path)
            print("Loaded Generator weights")

        if load_discriminator_weights:
            self.discriminative_network.load_gan_weights(self.srgan_model_)

        datagen = ImageDataGenerator(rescale=1. / 255)
        img_width = self.img_width * 4
        img_height = self.img_height * 4

        early_stop = False
        iteration = 0
        prev_improvement = -1

        if save_loss:
            if pre_train_srgan:
                loss_history = {'generator_loss' : [],
                                'val_psnr' : [], }
            elif pre_train_discriminator:
                loss_history = {'discriminator_loss' : [],
                                'discriminator_acc' : [], }
            else:
                loss_history = {'discriminator_loss' : [],
                                'discriminator_acc' : [],
                                'generator_loss' : [],
                                'val_psnr': [], }

        y_vgg_dummy = np.zeros((self.batch_size * 2, 3, img_width // 32, img_height // 32)) # 5 Max Pools = 2 ** 5 = 32

        if not pre_train_srgan:
            y_gan = [0] * self.batch_size + [1] * self.batch_size
            y_gan = np.asarray(y_gan, dtype=np.float32).reshape(-1, 1)

        print("Training SRGAN network")
        for i in range(nb_epochs):
            print()
            print("Epoch : %d" % (i + 1))

            for x in datagen.flow_from_directory(image_dir, class_mode=None, batch_size=self.batch_size,
                                                 target_size=(img_width, img_height)):
                try:
                    t1 = time.time()

                    if not pre_train_srgan and not pre_train_discriminator:
                        x_vgg = x.copy() * 255 # VGG input [0 - 255 scale]

                    # resize images
                    x_temp = x.copy()
                    x_temp = x_temp.transpose((0, 2, 3, 1))

                    x_generator = np.empty((self.batch_size, self.img_width, self.img_height, 3))

                    for j in range(self.batch_size):
                        img = gaussian_filter(x_temp[j], sigma=1.0)
                        img = imresize(img, (self.img_width, self.img_height), interp='bicubic')
                        x_generator[j, :, :, :] = img

                    x_generator = x_generator.transpose((0, 3, 1, 2))

                    if iteration % 50 == 0 and iteration != 0 and not pre_train_discriminator:
                        print("Validation image..")
                        output_image_batch = self.generative_network.get_generator_output(x_generator,
                                                                                          self.srgan_model_)
                        if type(output_image_batch) == list:
                            output_image_batch = output_image_batch[0]

                        average_psnr = 0.0
                        for x_i in range(self.batch_size):
                            average_psnr += psnr(x[x_i], np.clip(output_image_batch[x_i], 0, 255) / 255.)

                        average_psnr /= self.batch_size

                        if save_loss:
                            loss_history['val_psnr'].append(average_psnr)

                        iteration += self.batch_size
                        t2 = time.time()

                        print("Time required : %0.2f. Average validation PSNR over %d samples = %0.2f" %
                              (t2 - t1, self.batch_size, average_psnr))

                        for x_i in range(self.batch_size):
                            real_path = "val_images/epoch_%d_iteration_%d_num_%d_real_.png" % (i + 1, iteration, x_i + 1)
                            generated_path = "val_images/epoch_%d_iteration_%d_num_%d_generated.png" % (i + 1,
                                                                                                        iteration,
                                                                                                        x_i + 1)

                            val_x = x[x_i].copy() * 255.
                            val_x = val_x.transpose((1, 2, 0))
                            val_x = np.clip(val_x, 0, 255).astype('uint8')

                            output_image = output_image_batch[x_i]
                            output_image = output_image.transpose((1, 2, 0))
                            output_image = np.clip(output_image, 0, 255).astype('uint8')

                            imsave(real_path, val_x)
                            imsave(generated_path, output_image)

                        '''
                        Don't train of validation images for now.

                        Note that if nb_epochs > 1, there is a chance that
                        validation images may be used for training purposes as well.

                        In that case, this isn't strictly a validation measure, instead of
                        just a check to see what the network has learned.
                        '''
                        continue

                    if pre_train_srgan:
                        # Train only generator + vgg network

                        # Use custom bypass_fit to bypass the check for same input and output batch size
                        hist = bypass_fit(self.srgan_model_, [x_generator, x * 255], y_vgg_dummy,
                                                     batch_size=self.batch_size, nb_epoch=1, verbose=0)
                        sr_loss = hist.history['loss'][0]

                        if save_loss:
                            loss_history['generator_loss'].append(sr_loss)

                        if prev_improvement == -1:
                            prev_improvement = sr_loss

                        improvement = (prev_improvement - sr_loss) / prev_improvement * 100
                        prev_improvement = sr_loss

                        iteration += self.batch_size
                        t2 = time.time()

                        print("Iter : %d / %d | Improvement : %0.2f percent | Time required : %0.2f seconds | "
                              "Generative Loss : %0.3f" % (iteration, nb_images, improvement, t2 - t1, sr_loss))
                    elif pre_train_discriminator:
                        # Train only discriminator
                        X_pred = self.generative_model_.predict(x_generator, self.batch_size)
                        X_pred /= 255

                        X = np.concatenate((X_pred, x))

                        hist = self.discriminative_model_.fit(X, y_gan, batch_size=self.batch_size * 2,
                                                              nb_epoch=1, verbose=0)

                        discriminator_loss = hist.history['loss'][0]

                        if save_loss:
                            loss_history['discriminator_loss'].append(discriminator_loss)

                        if prev_improvement == -1:
                            prev_improvement = discriminator_loss

                        improvement = (prev_improvement - discriminator_loss) / prev_improvement * 100
                        prev_improvement = discriminator_loss

                        iteration += self.batch_size
                        t2 = time.time()

                        print("Iter : %d / %d | Improvement : %0.2f percent | Time required : %0.2f seconds | "
                            "Discriminator Loss : %0.2f" % (iteration, nb_images,
                                                            improvement, t2 - t1,
                                                            discriminator_loss))

                    else:
                        # Train only discriminator, disable training of srgan
                        self.discriminative_network.set_trainable(self.srgan_model_, value=True)
                        self.generative_network.set_trainable(self.srgan_model_, value=False)

                        # Use custom bypass_fit to bypass the check for same input and output batch size
                        # hist = bypass_fit(self.srgan_model_, [x_generator, x * 255, x_vgg],
                        #                          [y_gan, y_vgg_dummy],
                        #                          batch_size=self.batch_size, nb_epoch=1, verbose=0)

                        X_pred = self.generative_model_.predict(x, self.batch_size)
                        X_pred /= 255

                        X = np.concatenate((X_pred, x))

                        hist = self.discriminative_model_.fit(X, y_gan, verbose=0,
                                                              batch_size=self.batch_size * 2, nb_epoch=1)

                        discriminator_loss = hist.history['loss'][0]
                        discriminator_acc = hist.history['acc'][0]

                        # Train only generator, disable training of discriminator
                        self.discriminative_network.set_trainable(self.srgan_model_, value=False)
                        self.generative_network.set_trainable(self.srgan_model_, value=True)

                        # Use custom bypass_fit to bypass the check for same input and output batch size
                        hist = bypass_fit(self.srgan_model_, [x_generator[:self.batch_size, :, :, :], x_vgg],
                                                 [y_gan, y_vgg_dummy],
                                                 batch_size=self.batch_size, nb_epoch=1, verbose=0)

                        generative_loss = hist.history['loss'][0]

                        if save_loss:
                            loss_history['discriminator_loss'].append(discriminator_loss)
                            loss_history['generator_loss'].append(generative_loss)

                        if prev_improvement == -1:
                            prev_improvement = discriminator_loss

                        improvement = (prev_improvement - discriminator_loss) / prev_improvement * 100
                        prev_improvement = discriminator_loss

                        iteration += self.batch_size
                        t2 = time.time()
                        print("Iter : %d / %d | Improvement : %0.2f percent | Time required : %0.2f seconds | "
                              "Discriminator Loss : %0.3f | Generative Loss : %0.3f" %
                              (iteration, nb_images, improvement, t2 - t1, discriminator_loss, generative_loss))

                    if iteration % 1000 == 0 and iteration != 0:
                        print("Saving model weights.")
                        # Save predictive (SR network) weights
                        self._save_model_weights(pre_train_srgan, pre_train_discriminator)
                        self._save_loss_history(loss_history, pre_train_srgan, pre_train_discriminator, save_loss)

                    if iteration >= nb_images:
                        break

                except KeyboardInterrupt:
                    print("Keyboard interrupt detected. Stopping early.")
                    early_stop = True
                    break

            iteration = 0

            if early_stop:
                break

        print("Finished training SRGAN network. Saving model weights.")
        # Save predictive (SR network) weights
        self._save_model_weights(pre_train_srgan, pre_train_discriminator)
        self._save_loss_history(loss_history, pre_train_srgan, pre_train_discriminator, save_loss)

    def _save_model_weights(self, pre_train_srgan, pre_train_discriminator):
        if not pre_train_discriminator:
            self.generative_model_.save_weights(self.generative_network.sr_weights_path, overwrite=True)

        if not pre_train_srgan:
            # Save GAN (discriminative network) weights
            self.discriminative_network.save_gan_weights(self.discriminative_model_)

    def _save_loss_history(self, loss_history, pre_train_srgan, pre_train_discriminator, save_loss):
        if save_loss:
            print("Saving loss history")

            if pre_train_srgan:
                with open('pretrain losses - srgan.json', 'w') as f:
                    json.dump(loss_history, f)
            elif pre_train_discriminator:
                with open('pretrain losses - discriminator.json', 'w') as f:
                    json.dump(loss_history, f)
            else:
                with open('fulltrain losses.json', 'w') as f:
                    json.dump(loss_history, f)

            print("Saved loss history")


if __name__ == "__main__":
    # Path to MS COCO dataset
    coco_path = r"D:\Yue\Documents\Dataset\coco2014\train2014"

    '''
    Base Network manager for the SRGAN model

    Width / Height = 32 to reduce the memory requirement for the discriminator.

    Batch size = 1 is slower, but uses the least amount of gpu memory, and also acts as
    Instance Normalization (batch norm with 1 input image) which speeds up training slightly.
    '''
    srgan_network = SRGANNetwork(img_width=32, img_height=32, batch_size=1)

    # Pretrain the SRGAN network
    srgan_network.pre_train_srgan(coco_path, nb_images=50000, nb_epochs=1)

    # Pretrain the discriminator network
    srgan_network.pre_train_discriminator(coco_path, nb_images=50000, nb_epochs=1)

    # Fully train the SRGAN with VGG loss and Discriminator loss
    srgan_network.train_full_model(coco_path, nb_images=80000, nb_epochs=10)











