
import sys

import numpy as np
from collections import OrderedDict

import theano
import theano.tensor as T
from theano.tensor.shared_randomstreams import RandomStreams

from layers.gating import SimplifiedLstm, SimplifiedLstmDecoder, BidirectionSLstm, VanillaLstm, VanillaLstmDecoder, BidirectionLstm, VanillaRNN, VanillaRNNDecoder, SimplifiedGRU, GatedRecurrentUnit, LstmNoPeepholes, LstmNOG, LstmNIG, LstmNFG
from layers.layers import GeneralLayer, LinearLayer, SigmoidLayer
from layers.recurrent_output_layer import RecurrentOutputLayer
from layers.lhuc_layer import SigmoidLayer_LHUC, VanillaLstm_LHUC

from training_schemes.rprop import compile_RPROP_train_function
from training_schemes.adam_v2 import compile_ADAM_train_function

from models.seq2seq import VanillaSequenceEncoder, DistributedSequenceEncoder

import logging

class DeepEncoderDecoderNetwork(object):
    """
    This class is to assemble various neural network architectures. From basic feedforward neural network to bidirectional gated recurrent neural networks and hybrid architecture. **Hybrid** means a combination of feedforward and recurrent architecture.

    """


    def __init__(self, n_in, hidden_layer_size, n_out, L1_reg, L2_reg, hidden_layer_type, output_type='LINEAR', network_type='S2S', ed_type='HED', dropout_rate=0.0, optimizer='sgd', MLU_div_lengths = [], loss_function='MMSE', rnn_batch_training=False):
        """ This function initialises a neural network

        :param n_in: Dimensionality of input features
        :type in: Integer
        :param hidden_layer_size: The layer size for each hidden layer
        :type hidden_layer_size: A list of integers
        :param n_out: Dimensionality of output features
        :type n_out: Integrer
        :param hidden_layer_type: the activation types of each hidden layers, e.g., TANH, LSTM, GRU, BLSTM
        :param L1_reg: the L1 regulasation weight
        :param L2_reg: the L2 regulasation weight
        :param output_type: the activation type of the output layer, by default is 'LINEAR', linear regression.
        :param dropout_rate: probability of dropout, a float number between 0 and 1.
        """

        logger = logging.getLogger("merlin.DNN initialization")

        self.n_in = int(n_in)
        self.n_out = int(n_out)

        self.n_layers = len(hidden_layer_size)

        self.dropout_rate = dropout_rate
        self.optimizer = optimizer
        self.loss_function = loss_function
        self.is_train = T.iscalar('is_train')
        self.rnn_batch_training = rnn_batch_training

        assert len(hidden_layer_size) == len(hidden_layer_type)

        self.list_of_activations = ['TANH', 'SIGMOID', 'SOFTMAX', 'RELU', 'RESU']
        
        BLSTM_variants   = ['BLSTM', 'BSLSTM', 'BLSTME', 'BSLSTME']
        Encoder_variants = ['RNNE', 'LSTME', 'BLSTME', 'SLSTME', 'TANHE']
        Decoder_variants = ['RNND', 'LSTMD', 'SLSTMD']

        if self.rnn_batch_training:
            self.x = T.tensor3('x')
            self.y = T.tensor3('y')
        else:
            self.x = T.matrix('x')
            self.y = T.matrix('y')
        
        if network_type == "S2S":
            self.d = T.ivector('d')
            self.f = T.matrix('f')

        self.L1_reg = L1_reg
        self.L2_reg = L2_reg

        self.rnn_layers = []
        self.params = []
        self.delta_params = []

        rng = np.random.RandomState(123)

        prev_seg_end = 0
        encoder_count = 0
        MLU_div = MLU_div_lengths
        for i in range(self.n_layers):
            if i == 0:
                input_size = n_in
            else:
                input_size = hidden_layer_size[i-1]
                if hidden_layer_type[i-1] in BLSTM_variants:
                    input_size = hidden_layer_size[i-1]*2

            if i == 0:
                layer_input = self.x
            else:
                layer_input = self.rnn_layers[i-1].output
            
            ### sequence-to-sequence mapping ###
            if hidden_layer_type[i-1] in Encoder_variants:
                dur_input        = self.d
                frame_feat_input = self.f

                # vanilla encoder-decoder (phone-level features)
                if ed_type == "VED":
                    seq2seq_model = DistributedSequenceEncoder(rng, layer_input, dur_input)
                    layer_input   = T.concatenate((seq2seq_model.encoded_output, frame_feat_input), axis=1)
                    input_size    = input_size+4
                # hierarchical encoder-decoder
                elif ed_type == "HED":
                    seg_len       = layer_input.size//input_size
                    seg_dur_input = dur_input[prev_seg_end: prev_seg_end+seg_len]
                    num_of_segs   = T.sum(seg_dur_input)
                    seq2seq_model = DistributedSequenceEncoder(rng, layer_input, seg_dur_input)
                    addfeat_input = frame_feat_input[0:num_of_segs, MLU_div[encoder_count]:MLU_div[encoder_count+1]]  
                    layer_input   = T.concatenate((seq2seq_model.encoded_output, addfeat_input), axis=1)
                    input_size    = input_size + (MLU_div[encoder_count+1]-MLU_div[encoder_count])
                    prev_seg_end  = prev_seg_end + seg_len
                    encoder_count = encoder_count + 1

            # hidden layer activation
            if hidden_layer_type[i] in self.list_of_activations:
                hidden_activation = hidden_layer_type[i].lower()
                hidden_layer = GeneralLayer(rng, layer_input, input_size, hidden_layer_size[i], activation=hidden_activation, p=self.dropout_rate, training=self.is_train)
            elif hidden_layer_type[i] == 'TANHE' or hidden_layer_type[i] == 'SIGMOIDE':
                hidden_activation = hidden_layer_type[i][0:-1].lower()
                hidden_layer = GeneralLayer(rng, layer_input, input_size, hidden_layer_size[i], activation=hidden_activation, p=self.dropout_rate, training=self.is_train)
            elif hidden_layer_type[i] == 'TANH_LHUC':
                hidden_layer = SigmoidLayer_LHUC(rng, layer_input, input_size, hidden_layer_size[i], activation=T.tanh, p=self.dropout_rate, training=self.is_train)
            elif hidden_layer_type[i] == 'SLSTM' or hidden_layer_type[i] == 'SLSTME':
                hidden_layer = SimplifiedLstm(rng, layer_input, input_size, hidden_layer_size[i], p=self.dropout_rate, training=self.is_train, rnn_batch_training=self.rnn_batch_training)
            elif hidden_layer_type[i] == 'SLSTMD':
                hidden_layer = SimplifiedLstmDecoder(rng, layer_input, input_size, hidden_layer_size[i], self.n_out, p=self.dropout_rate, training=self.is_train, rnn_batch_training=self.rnn_batch_training)
            elif hidden_layer_type[i] == 'SGRU':
                hidden_layer = SimplifiedGRU(rng, layer_input, input_size, hidden_layer_size[i], p=self.dropout_rate, training=self.is_train, rnn_batch_training=self.rnn_batch_training)
            elif hidden_layer_type[i] == 'GRU':
                hidden_layer = GatedRecurrentUnit(rng, layer_input, input_size, hidden_layer_size[i], p=self.dropout_rate, training=self.is_train, rnn_batch_training=self.rnn_batch_training)
            elif hidden_layer_type[i] == 'LSTM' or hidden_layer_type[i] == 'LSTME':
                hidden_layer = VanillaLstm(rng, layer_input, input_size, hidden_layer_size[i], p=self.dropout_rate, training=self.is_train, rnn_batch_training=self.rnn_batch_training)
            elif hidden_layer_type[i] == 'LSTMD':
                hidden_layer = VanillaLstmDecoder(rng, layer_input, input_size, hidden_layer_size[i], self.n_out, p=self.dropout_rate, training=self.is_train, rnn_batch_training=self.rnn_batch_training)
            elif hidden_layer_type[i] == 'BSLSTM' or hidden_layer_type[i] == 'BSLSTME':
                hidden_layer = BidirectionSLstm(rng, layer_input, input_size, hidden_layer_size[i], hidden_layer_size[i], p=self.dropout_rate, training=self.is_train, rnn_batch_training=self.rnn_batch_training)
            elif hidden_layer_type[i] == 'BLSTM' or hidden_layer_type[i] == 'BLSTME':
                hidden_layer = BidirectionLstm(rng, layer_input, input_size, hidden_layer_size[i], hidden_layer_size[i], p=self.dropout_rate, training=self.is_train, rnn_batch_training=self.rnn_batch_training)
            elif hidden_layer_type[i] == 'RNN' or hidden_layer_type[i] == 'RNNE':
                hidden_layer = VanillaRNN(rng, layer_input, input_size, hidden_layer_size[i], p=self.dropout_rate, training=self.is_train, rnn_batch_training=self.rnn_batch_training)
            elif hidden_layer_type[i] == 'RNND':
                hidden_layer = VanillaRNNDecoder(rng, layer_input, input_size, hidden_layer_size[i], self.n_out, p=self.dropout_rate, training=self.is_train, rnn_batch_training=self.rnn_batch_training)
            elif hidden_layer_type[i] == 'LSTM_LHUC':
                hidden_layer = VanillaLstm_LHUC(rng, layer_input, input_size, hidden_layer_size[i], p=self.dropout_rate, training=self.is_train, rnn_batch_training=self.rnn_batch_training)
            else:
                logger.critical("This hidden layer type: %s is not supported right now! \n Please use one of the following: SLSTM, BSLSTM, TANH, SIGMOID\n" %(hidden_layer_type[i]))
                sys.exit(1)

            self.rnn_layers.append(hidden_layer)
            self.params.extend(hidden_layer.params)

        input_size = hidden_layer_size[-1]
        if hidden_layer_type[-1] in BLSTM_variants:
            input_size = hidden_layer_size[-1]*2

        if hidden_layer_type[-1] in Decoder_variants:
            self.final_layer = self.rnn_layers[-1]
        else:
            output_activation = output_type.lower()
            if output_activation == 'linear':
                self.final_layer = LinearLayer(rng, self.rnn_layers[-1].output, input_size, self.n_out)
            elif output_activation == 'recurrent':
                self.final_layer = RecurrentOutputLayer(rng, self.rnn_layers[-1].output, input_size, self.n_out, rnn_batch_training=self.rnn_batch_training)
            elif output_type.upper() in self.list_of_activations:
                self.final_layer = GeneralLayer(rng, self.rnn_layers[-1].output, input_size, self.n_out, activation=output_activation)
            else:
                logger.critical("This output layer type: %s is not supported right now! \n Please use one of the following: LINEAR, BSLSTM\n" %(output_type))
                sys.exit(1)

            self.params.extend(self.final_layer.params)

        self.updates = {}
        for param in self.params:
            self.updates[param] = theano.shared(value = np.zeros(param.get_value(borrow = True).shape,
                                                dtype = theano.config.floatX), name = 'updates')

        if self.loss_function == 'CCE':
            self.finetune_cost = self.categorical_crossentropy_loss(self.final_layer.output, self.y) 
            self.errors        = self.categorical_crossentropy_loss(self.final_layer.output, self.y) 
        elif self.loss_function == 'Hinge':    
            self.finetune_cost = self.multiclass_hinge_loss(self.final_layer.output, self.y)
            self.errors        = self.multiclass_hinge_loss(self.final_layer.output, self.y)
        elif self.loss_function == 'MMSE':
            if self.rnn_batch_training:
                self.y_mod = T.reshape(self.y, (-1, n_out))
                self.final_layer_output = T.reshape(self.final_layer.output, (-1, n_out))

                nonzero_rows = T.any(self.y_mod, 1).nonzero()
            
                self.y_mod = self.y_mod[nonzero_rows]
                self.final_layer_output = self.final_layer_output[nonzero_rows]
            
                self.finetune_cost = T.mean(T.sum((self.final_layer_output - self.y_mod) ** 2, axis=1))
                self.errors = T.mean(T.sum((self.final_layer_output - self.y_mod) ** 2, axis=1))
            else:
                self.finetune_cost = T.mean(T.sum((self.final_layer.output - self.y) ** 2, axis=1))
                self.errors = T.mean(T.sum((self.final_layer.output - self.y) ** 2, axis=1))

    def categorical_crossentropy_loss(self, predictions, targets):
        return T.nnet.categorical_crossentropy(predictions, targets).mean()

    def multiclass_hinge_loss(self, predictions, targets, delta=1):
        num_cls = predictions.shape[1]
        if targets.ndim == predictions.ndim - 1:
            targets = T.extra_ops.to_one_hot(targets, num_cls)
        elif targets.ndim != predictions.ndim:
            raise TypeError('rank mismatch between targets and predictions')
        corrects = predictions[targets.nonzero()]
        rest = T.reshape(predictions[(1-targets).nonzero()],
                                 (-1, num_cls-1))
        rest = T.max(rest, axis=1)
        return T.nnet.relu(rest - corrects + delta).mean()


    def build_finetune_functions(self, train_shared_xy, valid_shared_xy, use_lhuc=False, layer_index=0):
        """ This function is to build finetune functions and to update gradients

        :param train_shared_xy: theano shared variable for input and output training data
        :type train_shared_xy: tuple of shared variable
        :param valid_shared_xy: theano shared variable for input and output development data
        :type valid_shared_xy: tuple of shared variable
        :returns: finetune functions for training and development

        """

        logger = logging.getLogger("merlin.DNN initialization")

        (train_set_x, train_set_y) = train_shared_xy
        (valid_set_x, valid_set_y) = valid_shared_xy

        lr = T.scalar('lr', dtype = theano.config.floatX)
        mom = T.scalar('mom', dtype = theano.config.floatX)  # momentum

        cost = self.finetune_cost #+ self.L2_reg * self.L2_sqr
        
        ## added for LHUC
        if use_lhuc:
            # In lhuc the parameters are only scaling parameters which have the name 'c'
            self.lhuc_params = []
            for p in self.params:
                if p.name == 'c':
                    self.lhuc_params.append(p)
            params = self.lhuc_params
            gparams = T.grad(cost, params)
        else:
            params = self.params
            gparams = T.grad(cost, params)


        freeze_params = 0
        for layer in range(layer_index):
            freeze_params += len(self.rnn_layers[layer].params)

        # use optimizer
        if self.optimizer=='sgd':
            # zip just concatenate two lists
            updates = OrderedDict()

            for i, (param, gparam) in enumerate(zip(params, gparams)):
                weight_update = self.updates[param]
                upd = mom * weight_update - lr * gparam
                updates[weight_update] = upd

                # freeze layers and update weights
                if i >= freeze_params:
                    updates[param] = param + upd

        elif self.optimizer=='adam':
            updates = compile_ADAM_train_function(self, gparams, learning_rate=lr)
        elif self.optimizer=='rprop':
            updates = compile_RPROP_train_function(self, gparams)
        else: 
            logger.critical("This optimizer: %s is not supported right now! \n Please use one of the following: sgd, adam, rprop\n" %(self.optimizer))
            sys.exit(1)

        train_model = theano.function(inputs = [lr, mom],  #index, batch_size
                                      outputs = self.errors,
                                      updates = updates,
                                      givens = {self.x: train_set_x, #[index*batch_size:(index + 1)*batch_size]
                                                self.y: train_set_y,
                                                self.is_train: np.cast['int32'](1)}, on_unused_input='ignore')


        valid_model = theano.function(inputs = [],
                                      outputs = self.errors,
                                      givens = {self.x: valid_set_x,
                                                self.y: valid_set_y,
                                                self.is_train: np.cast['int32'](0)}, on_unused_input='ignore')

        return  train_model, valid_model

    def build_finetune_functions_S2S(self, train_shared_xyd, valid_shared_xyd):
        """ This function is to build finetune functions and to update gradients
        
        :param train_shared_xy: theano shared variable for input and output training data 
        :type train_shared_xy: tuple of shared variable
        :param valid_shared_xy: theano shared variable for input and output development data
        :type valid_shared_xy: tuple of shared variable
        :returns: finetune functions for training and development
        
        """

        (train_set_x, train_set_y, train_set_d) = train_shared_xyd
        (valid_set_x, valid_set_y, valid_set_d) = valid_shared_xyd

        lr = T.scalar('lr', dtype = theano.config.floatX)
        mom = T.scalar('mom', dtype = theano.config.floatX)  # momentum

        cost = self.finetune_cost #+ self.L2_reg * self.L2_sqr

        gparams = T.grad(cost, self.params)


        # zip just concatenate two lists
        updates = OrderedDict()

        for param, gparam in zip(self.params, gparams):
            weight_update = self.updates[param]
            upd = mom * weight_update - lr * gparam
            updates[weight_update] = upd
            updates[param] = param + upd

        train_model = theano.function(inputs = [lr, mom],  
                                      outputs = self.errors,
                                      updates = updates,
                                      givens = {self.x: train_set_x, 
                                                self.y: train_set_y,
                                                self.d: train_set_d,
                                                self.is_train: np.cast['int32'](1)}, on_unused_input='ignore')


        valid_model = theano.function(inputs = [],
                                      outputs = self.errors,
                                      givens = {self.x: valid_set_x,
                                                self.y: valid_set_y,
                                                self.d: valid_set_d,
                                                self.is_train: np.cast['int32'](0)}, on_unused_input='ignore')
 
        return  train_model, valid_model

    def build_finetune_functions_S2SPF(self, train_shared_xydf, valid_shared_xydf, layer_index=6):
        """ This function is to build finetune functions and to update gradients
        
        :param train_shared_xy: theano shared variable for input and output training data 
        :type train_shared_xy: tuple of shared variable
        :param valid_shared_xy: theano shared variable for input and output development data
        :type valid_shared_xy: tuple of shared variable
        :returns: finetune functions for training and development
        
        """

        (train_set_x, train_set_y, train_set_d, train_set_f) = train_shared_xydf
        (valid_set_x, valid_set_y, valid_set_d, valid_set_f) = valid_shared_xydf

        lr = T.scalar('lr', dtype = theano.config.floatX)
        mom = T.scalar('mom', dtype = theano.config.floatX)  # momentum

        cost = self.finetune_cost #+ self.L2_reg * self.L2_sqr

        params = self.params
        gparams = T.grad(cost, params)

        encoder_params = 0
        for layer in range(layer_index):
            encoder_params += len(self.rnn_layers[layer].params)

        # use optimizer
        if self.optimizer=='sgd':
            # zip just concatenate two lists
            updates = OrderedDict()

            for i, (param, gparam) in enumerate(zip(params, gparams)):
                weight_update = self.updates[param]
                if i >= encoder_params:
                    upd = mom * weight_update - lr * gparam
                else:
                    upd = mom * weight_update - (lr*2) * gparam
                updates[weight_update] = upd
                updates[param] = param + upd

        elif self.optimizer=='adam':
            updates = compile_ADAM_train_function(self, gparams, learning_rate=lr)
        elif self.optimizer=='rprop':
            updates = compile_RPROP_train_function(self, gparams)
        else: 
            logger.critical("This optimizer: %s is not supported right now! \n Please use one of the following: sgd, adam, rprop\n" %(self.optimizer))
            sys.exit(1)


        train_model = theano.function(inputs = [lr, mom],  
                                      outputs = self.errors,
                                      updates = updates,
                                      givens = {self.x: train_set_x, 
                                                self.y: train_set_y,
                                                self.d: train_set_d,
                                                self.f: train_set_f,
                                                self.is_train: np.cast['int32'](1)}, on_unused_input='ignore')


        valid_model = theano.function(inputs = [],
                                      outputs = self.errors,
                                      givens = {self.x: valid_set_x,
                                                self.y: valid_set_y,
                                                self.d: valid_set_d,
                                                self.f: valid_set_f,
                                                self.is_train: np.cast['int32'](0)}, on_unused_input='ignore')
 
        return  train_model, valid_model

    def parameter_prediction(self, test_set_x):  #, batch_size
        """ This function is to predict the output of NN
        
        :param test_set_x: input features for a testing sentence
        :type test_set_x: python array variable
        :returns: predicted features
        
        """
    

        n_test_set_x = test_set_x.shape[0]

        test_out = theano.function([], self.final_layer.output,
              givens={self.x: test_set_x[0:n_test_set_x], self.is_train: np.cast['int32'](0)}, on_unused_input='ignore')

        predict_parameter = test_out()

        return predict_parameter    

    def parameter_prediction_S2S(self, test_set_x, test_set_d):  
        """ This function is to predict the output of NN
        
        :param test_set_x: input features for a testing sentence
        :param test_set_d: phone durations for a testing sentence
        :type test_set_x: python array variable
        :type test_set_d: python array variable
        :returns: predicted features
        
        """

        n_test_set_x = test_set_x.shape[0]

        test_out = theano.function([], self.final_layer.output,
                givens={self.x: test_set_x[0:n_test_set_x], self.d: test_set_d[0:n_test_set_x], self.is_train: np.cast['int32'](0)}, on_unused_input='ignore')

        predict_parameter = test_out()

        return predict_parameter

    def parameter_prediction_S2SPF(self, test_set_x, test_set_d, test_set_f):  
        """ This function is to predict the output of NN
        
        :param test_set_x: input features for a testing sentence
        :param test_set_d: phone durations for a testing sentence
        :type test_set_x: python array variable
        :type test_set_d: python array variable
        :returns: predicted features
        
        """

        n_test_set_x  = test_set_x.shape[0]
        num_of_frames = sum(test_set_d)

        test_out = theano.function([], self.final_layer.output,
                givens={self.x: test_set_x[0:n_test_set_x], self.d: test_set_d, self.f: test_set_f, self.is_train: np.cast['int32'](0)}, on_unused_input='ignore')

        predict_parameter = test_out()

        return predict_parameter

    def parameter_prediction_CTC(self, test_set_x):  #, batch_size

        n_test_set_x = test_set_x.shape[0]

        test_out = theano.function([], self.rnn_layers[-1].output,
              givens={self.x: test_set_x[0:n_test_set_x]})

        predict_parameter = test_out()

        return predict_parameter

    def parameter_prediction_MDN(self, test_set_x):  #, batch_size

        n_test_set_x = test_set_x.get_value(borrow=True).shape[0]

        test_out = theano.function([], self.final_layer.mu,
              givens={self.x: test_set_x[0:n_test_set_x]})

        predict_parameter = test_out()

        return predict_parameter

    def parameter_prediction_mix(self, test_set_x):  #, batch_size

        n_test_set_x = test_set_x.get_value(borrow=True).shape[0]

        test_out = theano.function([], self.final_layer.mix,
              givens={self.x: test_set_x[0:n_test_set_x]})

        predict_parameter = test_out()

        return predict_parameter

    def parameter_prediction_sigma(self, test_set_x):  #, batch_size

        n_test_set_x = test_set_x.get_value(borrow=True).shape[0]

        test_out = theano.function([], self.final_layer.sigma,
              givens={self.x: test_set_x[0:n_test_set_x]})

        predict_parameter = test_out()

        return predict_parameter
    
    ## the function to output activations at a hidden layer
    def generate_hidden_layer(self, test_set_x, bn_layer_index):
        """ This function is to predict the bottleneck features of NN

        :param test_set_x: input features for a testing sentence
        :type test_set_x: python array variable
        :returns: predicted bottleneck features

        """

        n_test_set_x = test_set_x.shape[0]

        test_out = theano.function([], self.rnn_layers[bn_layer_index].output,
                givens={self.x: test_set_x, self.is_train: np.cast['int32'](0)}, on_unused_input='ignore')

        predict_parameter = test_out()

        return predict_parameter

