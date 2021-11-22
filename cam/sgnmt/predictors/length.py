# -*- coding: utf-8 -*-
# coding=utf-8
# Copyright 2019 The SGNMT Authors.
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

"""This module contains predictors that deal wit the length of the
target sentence. The ``NBLengthPredictor`` assumes a negative binomial
distribution on the target sentence lengths, where the parameters r and
p are linear combinations of features extracted from the source 
sentence. The ``WordCountPredictor`` adds the number of words as cost,
which can be used to prevent hypotheses from getting to short when 
using a language model.
"""

import logging
import math
from scipy.special import logsumexp
from scipy.special import gammaln

from cam.sgnmt import utils
from cam.sgnmt.misc.trie import SimpleTrie
from cam.sgnmt.predictors.core import Predictor, UnboundedVocabularyPredictor
import numpy as np


NUM_FEATURES = 5
EPS_R = 0.1;



def load_external_lengths(path):
    """Loads a length distribution from a plain text file. The file
    must contain blank separated <length>:<score> pairs in each line.
    
    Args:
        path (string): Path to the length file.
    
    Returns:
        list of dicts mapping a length to its scores, one dict for each
        sentence.
    """
    lengths = []
    with open(path) as f:
        for line in f:
            scores = {}
            for pair in line.strip().split():
                if ':' in pair:
                    length, score = pair.split(':')
                    scores[int(length)] = float(score)
                else:
                    scores[int(pair)] = 0.0
            lengths.append(scores)
    return lengths

def load_external_ids(path):
    """
    load file of ids to list
    """
    with open(path) as f:
       return [int(line.strip()) for line in f]

class NBLengthPredictor(Predictor):
    """This predictor assumes that target sentence lengths are 
    distributed according a negative binomial distribution with 
    parameters r,p. r is linear in features, p is the logistic of a
    linear function over the features. Weights can be trained using 
    the Matlab script ``estimate_length_model.m`` 
    
    Let w be the model_weights. All features are extracted from the
    src sentence::
    
      r = w0 * #char
      + w1 * #words
      + w2 * #punctuation
      + w3 * #char/#words
      + w4 * #punct/#words
      + w10
      
      p = logistic(w5 * #char
      + w6 * #words
      + w7 * #punctuation
      + w8 * #char/#words
      + w9 * #punct/#words
      + w11)
      
      target_length ~ NB(r,p)
      
    The biases w10 and w11 are optional.
    
    The predictor predicts EOS with NB(#consumed_words,r,p)
    """
    
    def __init__(self, text_file, model_weights, use_point_probs, offset = 0):
        """Creates a new target sentence length model predictor.
        
        Args:
            text_file (string): Path to the text file with the 
                                unindexed source sentences, i.e. not
                                using word ids
            model_weights (list): Weights w0 to w11 of the length 
                                  model. See class docstring for more
                                  information
            use_point_probs (bool): Use point estimates for EOS token,
                                    0.0 otherwise 
            offset (int): Subtract this from hypothesis length before
                          applying the NB model
        """
        super(NBLengthPredictor, self).__init__()
        self.use_point_probs = use_point_probs
        self.offset = offset
        if len(model_weights) == 2*NUM_FEATURES: # add biases
            model_weights.append(0.0)
            model_weights.append(0.0)
        if len(model_weights) != 2*NUM_FEATURES+2:
            logging.fatal("Number of length model weights has to be %d or %d"
                    % (2*NUM_FEATURES, 2*NUM_FEATURES+2))
        self.r_weights = model_weights[0:NUM_FEATURES] + [model_weights[-2]]
        self.p_weights = model_weights[NUM_FEATURES:2*NUM_FEATURES] + [model_weights[-1]]
        self.src_features = self._extract_features(text_file)
        self.n_consumed = 0 

    def _extract_features(self, file_name):
        """Extract all features from the source sentences. """
        feats = []
        with open(file_name) as f:
            for line in f:
                feats.append(self._analyse_sentence(line.strip()))
        return feats
    
    def _analyse_sentence(self, sentence):
        """Extract features for a single source sentence.
        
        Args:
            sentence (string): Source sentence string
        
        Returns:
            5-tuple of features as described in the class docstring
        """
        n_char = len(sentence) + 0.0
        n_words = len(sentence.split()) + 0.0
        n_punct = sum([sentence.count(s) for s in ",.:;-"]) + 0.0
        return [n_char, n_words, n_punct, n_char/n_words, n_punct/n_words]
        
    def get_unk_probability(self, posterior):
        """If we use point estimates, return 0 (=1). Otherwise, return
        the 1-p(EOS), with p(EOS) fetched from ``posterior``
        """
        if self.use_point_probs:
            if self.n_consumed == 0:
                return self.max_eos_prob
            return 0.0
        if self.n_consumed == 0:
            return 0.0
        return np.log(1.0 - np.exp(posterior[utils.EOS_ID]))
    
    def predict_next(self):
        """Returns a dictionary with single entry for EOS. """
        if self.n_consumed == 0:
            return {utils.EOS_ID : utils.NEG_INF}
        return {utils.EOS_ID : self._get_eos_prob()}
    
    def _get_eos_prob(self):
        """Get loglikelihood according cur_p, cur_r, and n_consumed """
        eos_point_prob = self._get_eos_point_prob(max(
                                              1, 
                                              self.n_consumed - self.offset))
        if self.use_point_probs:
            return eos_point_prob - self.max_eos_prob
        if not self.prev_eos_probs:
            self.prev_eos_probs.append(eos_point_prob)
            return eos_point_prob
        # bypass utils.log_sum because we always want to use logsumexp here 
        prev_sum = logsumexp(np.asarray([p for p in self.prev_eos_probs])) 
        self.prev_eos_probs.append(eos_point_prob)
        # Desired prob is eos_point_prob / (1-last_eos_probs_sum)
        return eos_point_prob - np.log(1.0-np.exp(prev_sum))
    
    def _get_eos_point_prob(self, n):
        return gammaln(n + self.cur_r) \
                - gammaln(n + 1) \
                - gammaln(self.cur_r) \
                + n * np.log(self.cur_p) \
                + self.cur_r * np.log(1.0-self.cur_p)
    
    def _get_max_eos_prob(self):
        """Get the maximum loglikelihood according cur_p, cur_r 
        TODO: replace this brute force impl. with something smarter
        """
        max_prob = utils.NEG_INF
        n_prob = max_prob
        n = 0
        while n_prob == max_prob:
            n += 1
            n_prob = self._get_eos_point_prob(n)
            max_prob = max(max_prob, n_prob)
        return max_prob
    
    def initialize(self, src_sentence):
        """Extract features for the source sentence. Note that this
        method does not use ``src_sentence`` as we need the string
        representation of the source sentence to extract features.
        
        Args:
            src_sentence (list): Not used
        """
        feat = self.src_features[self.current_sen_id] + [1.0]
        self.cur_r  = max(EPS_R, np.dot(feat, self.r_weights));
        p = np.dot(feat, self.p_weights)
        p = 1.0 / (1.0 + math.exp(-p))
        self.cur_p = max(utils.EPS_P, min(1.0 - utils.EPS_P, p))
        self.n_consumed = 0
        self.prev_eos_probs = []
        if self.use_point_probs:
            self.max_eos_prob = self._get_max_eos_prob()
    
    def consume(self, word):
        """Increases the current history length
        
        Args:
            word (int): Not used
        """
        self.n_consumed = self.n_consumed + 1
    
    def get_state(self):
        """State consists of the number of consumed words, and the
        accumulator for previous EOS probability estimates if we 
        don't use point estimates.
        """
        return self.n_consumed,self.prev_eos_probs
    
    def set_state(self, state):
        """Set the predictor state """
        self.n_consumed,self.prev_eos_probs = state

    def is_equal(self, state1, state2):
        """Returns true if the number of consumed words is the same """
        n1,_ = state1
        n2,_ = state2
        return n1 == n2


class WordCountPredictor(Predictor):
    """This predictor adds the (negative) number of words as feature.
    This means that this predictor encourages shorter hypotheses when
    used with a positive weight.
    """
    
    def __init__(self, word=-1,
                 nonterminal_penalty=False,
                 nonterminal_ids=None,
                 min_terminal_id=0,
                 max_terminal_id=30003,
                 negative_wc=True,
                 vocab_size=30003):
        """Creates a new word count predictor instance.
        
        Args:
            word (int): If this is non-negative we count only the
                        number of the specified word. If its
                        negative, count all words
            nonterminal_penalty (bool): If true, apply penalty only to 
                        tokens in a range  (the range *outside* 
                        min/max terminal id)
            nonterminal_ids: file containing ids of nonterminal tokens
            min_terminal_id: lower bound of tokens *not* to penalize,
                              if nonterminal_penalty selected
            max_terminal_id: upper bound of tokens *not* to penalize,
                             if nonterminal_penalty selected
            negative_wc: If true, the score of this predictor is the 
                         negative word count.
            vocab_size: upper bound of tokens, used to find nonterminal range

        """
        super(WordCountPredictor, self).__init__()
        val = 1.0
        if negative_wc:
          val = -1.0
        if nonterminal_penalty:
            if nonterminal_ids:
                nts = load_external_ids(nonterminal_ids)
            else:
                min_nt_range = range(0, min_terminal_id)
                max_nt_range = range(max_terminal_id + 1, vocab_size)
                nts = list(min_nt_range) + list(max_nt_range)
            self.posterior = {nt: val for nt in nts}
            self.posterior[utils.EOS_ID] = 0.0
            self.posterior[utils.UNK_ID] = 0.0
            self.unk_prob = 0.0
        elif word < 0:
            self.posterior = {utils.EOS_ID : 0.0}
            self.unk_prob = val
        else:
            self.posterior = {word : val}
            self.unk_prob = 0.0 
        
    def get_unk_probability(self, posterior):
        return self.unk_prob
    
    def predict_next(self):
        return self.posterior
    
    def initialize(self, src_sentence):
        """Empty"""
        pass
    
    def consume(self, word):
        """Empty"""
        pass
    
    def get_state(self):
        """Returns true """
        return True
    
    def set_state(self, state):
        """Empty"""
        pass

    def is_equal(self, state1, state2):
        """Returns true """
        return True


class WeightNonTerminalPredictor(Predictor):
    """This wrapper multiplies the weight of given tokens (those outside
    the min/max terminal range) by a factor."""
    
    def __init__(self, slave_predictor,
                 penalty_factor=1.0,
                 nonterminal_ids=None,
                 min_terminal_id=0,
                 max_terminal_id=30003,
                 vocab_size=30003):
        """Creates a new id-weighting wrapper for a predictor
        
        Args:
            slave_predictor: predictor to apply penalty to.
            penalty_factor (float): factor by which to multiply tokens in range
            min_terminal_id: lower bound of tokens *not* to penalize, 
        if nonterminal_penalty selected
            max_terminal_id: upper bound of tokens *not* to penalize,
        if nonterminal_penalty selected
            vocab_size: upper bound of tokens, used to find nonterminal range

        """
        super(WeightNonTerminalPredictor, self).__init__()
        if nonterminal_ids:
            nts = load_external_ids(nonterminal_ids)
        else:
            min_nt_range = range(0, min_terminal_id)
            max_nt_range = range(max_terminal_id + 1, vocab_size)
            nts = list(min_nt_range) + list(max_nt_range)
        self.slave_predictor = slave_predictor
        self.mult = {tok: penalty_factor for tok in nts}
        self.mult[utils.EOS_ID] = 1.0
        self.mult[utils.UNK_ID] = 1.0
        
    def get_unk_probability(self, posterior):
        return self.slave_predictor.get_unk_probability(posterior)
    
    def predict_next(self):
        posterior = self.slave_predictor.predict_next()
        post_keys = set(utils.common_viewkeys(posterior))
        for tok in self.mult:
            if tok in post_keys:
                posterior[tok] *= self.mult[tok]
        return posterior
    
    def initialize(self, src_sentence):
        self.slave_predictor.initialize(src_sentence)
    
    def consume(self, word):
        return self.slave_predictor.consume(word)
    
    def get_state(self):
        return self.slave_predictor.get_state()
    
    def set_state(self, state):
        self.slave_predictor.set_state(state)
    
    def is_equal(self, state1, state2):
        return self.slave_predictor.is_equal(state1, state2)



class ExternalLengthPredictor(Predictor):
    """This predictor loads the distribution over target sentence
    lengths from an external file. The file contains blank separated
    length:score pairs in each line which define the length 
    distribution. The predictor adds the specified scores directly
    to the EOS score.
    """
    
    def __init__(self, path):
        """Creates a external length distribution predictor.
        
        Args:
            path (string): Path to the file with target sentence length
                           distributions.
        """
        super(ExternalLengthPredictor, self).__init__()
        self.trg_lengths = load_external_lengths(path)
        
    def get_unk_probability(self, posterior):
        """Returns 0=log 1 if the partial hypothesis does not exceed
        max length. Otherwise, predict next returns an empty set,
        and we set everything else to -inf.
        """
        if self.n_consumed < self.max_length:
            return 0.0
        return utils.NEG_INF
    
    def predict_next(self):
        """Returns a dictionary with one entry and value 0 (=log 1). The
        key is either the next word in the target sentence or (if the
        target sentence has no more words) the end-of-sentence symbol.
        """
        if self.n_consumed in self.cur_scores: 
            return {utils.EOS_ID : self.cur_scores[self.n_consumed]}
        return {utils.EOS_ID : utils.NEG_INF} 
    
    def initialize(self, src_sentence):
        """Fetches the corresponding target sentence length 
        distribution and resets the word counter.
        
        Args:
            src_sentence (list):  Not used
        """
        self.cur_scores = self.trg_lengths[self.current_sen_id]
        self.max_length = max(self.cur_scores)
        self.n_consumed = 0

    def consume(self, word):
        """Increases word counter by one.
        
        Args:
            word (int): Not used
        """
        self.n_consumed = self.n_consumed + 1
    
    def get_state(self):
        """Returns the number of consumed words """
        return self.n_consumed
    
    def set_state(self, state):
        """Set the number of consumed words """
        self.n_consumed = state

    def is_equal(self, state1, state2):
        """Returns true if the number of consumed words is the same """
        return state1 == state2


class NgramCountPredictor(Predictor):
    """This predictor counts the number of n-grams in hypotheses. n-gram
    posteriors are loaded from a file. The predictor score is the sum of
    all n-gram posteriors in a hypothesis. """
    
    def __init__(self, path, order=0, discount_factor=-1.0):
        """Creates a new ngram count predictor instance.
        
        Args:
            path (string): Path to the n-gram posteriors. File format:
                           <ngram> : <score> (one ngram per line). Use
                           placeholder %d for sentence id.
            order (int): If positive, count n-grams of the specified
                         order. Otherwise, count all n-grams
            discount_factor (float): If non-negative, discount n-gram
                                     posteriors by this factor each time 
                                     they are consumed 
        """
        super(NgramCountPredictor, self).__init__()
        self.path = path 
        self.order = order
        self.discount_factor = discount_factor
        
    def get_unk_probability(self, posterior):
        """Always return 0.0 """
        return 0.0
    
    def predict_next(self):
        """Composes the posterior vector by collecting all ngrams which
        are consistent with the current history.
        """
        posterior = {}
        for i in reversed(range(len(self.cur_history)+1)):
            scores = self.ngrams.get(self.cur_history[i:])
            if scores:
                factors = False
                if self.discount_factor >= 0.0:
                    factors = self.discounts.get(self.cur_history[i:])
                if not factors:
                    for w,score in scores.items():
                        posterior[w] = posterior.get(w, 0.0) + score
                else:
                    for w,score in scores.items():
                        posterior[w] = posterior.get(w, 0.0) +  \
                                       factors.get(w, 1.0) * score
        return posterior
    
    def _load_posteriors(self, path):
        """Sets up self.max_history_len and self.ngrams """
        self.max_history_len = 0
        self.ngrams = SimpleTrie()
        logging.debug("Loading n-gram scores from %s..." % path)
        with open(path) as f:
            for line in f:
                ngram,score = line.split(':')
                words = [int(w) for w in ngram.strip().split()]
                if self.order > 0 and len(words) != self.order:
                    continue
                hist = words[:-1]
                last_word = words[-1]
                if last_word == utils.GO_ID:
                    continue
                self.max_history_len = max(self.max_history_len, len(hist))
                p = self.ngrams.get(hist)
                if p:
                    p[last_word] = float(score.strip())
                else:
                    self.ngrams.add(hist, {last_word: float(score.strip())})
    
    def initialize(self, src_sentence):
        """Loads n-gram posteriors and resets history.
        
        Args:
            src_sentence (list): not used
        """
        self._load_posteriors(utils.get_path(self.path, self.current_sen_id+1))
        self.cur_history = [utils.GO_ID]
        self.discounts = SimpleTrie()
    
    def consume(self, word):
        """Adds ``word`` to the current history. Shorten if the extended
        history exceeds ``max_history_len``.
        
        Args:
            word (int): Word to add to the history.
        """
        self.cur_history.append(word)
        if len(self.cur_history) > self.max_history_len:
            self.cur_history = self.cur_history[-self.max_history_len:]
        if self.discount_factor >= 0.0:
            for i in range(len(self.cur_history)):
                key = self.cur_history[i:-1]
                factors = self.discounts.get(key)
                if not factors:
                    factors = {word: self.discount_factor}
                else:
                    factors[word] = factors.get(word, 1.0)*self.discount_factor
                self.discounts.add(key, factors)
    
    def get_state(self):
        """Current history is the predictor state """
        return self.cur_history,self.discounts
    
    def set_state(self, state):
        """Current history is the predictor state """
        self.cur_history,self.discounts = state

    def is_equal(self, state1, state2):
        """Hypothesis recombination is
        not supported if discounting is enabled.
        """
        if self.discount_factor >= 0.0:
            return False
        hist1 = state1[0]
        hist2 = state2[0]
        if hist1 == hist2: # Return true if histories match
            return True
        if len(hist1) > len(hist2):
            hist_long = hist1
            hist_short = hist2
        else:
            hist_long = hist2
            hist_short = hist1
        min_len = len(hist_short)
        for n in range(1, min_len+1): # Look up non matching in self.ngrams
            key1 = hist1[-n:]
            key2 = hist2[-n:]
            if key1 != key2:
                if self.ngrams.get(key1) or self.ngrams.get(key2):
                    return False
        for n in range(min_len+1, len(hist_long)+1):
            if self.ngrams.get(hist_long[-n:]):
                return False
        return True


class UnkCountPredictor(Predictor):
    """This predictor regulates the number of UNKs in the output. We 
    assume that the number of UNKs in the target sentence is Poisson 
    distributed. This predictor is configured with n lambdas for
    0,1,...,>=n-1 UNKs in the source sentence. """
    
    def __init__(self, src_vocab_size, lambdas):
        """Initializes the UNK count predictor.

        Args:
            src_vocab_size (int): Size of source language vocabulary.
                                  Indices greater than this are 
                                  considered as UNK.
            lambdas (list): List of floats. The first entry is the 
                            lambda parameter given that the number of
                            unks in the source sentence is 0 etc. The
                            last float is lambda given that the source
                            sentence has more than n-1 unks.
        """
        self.lambdas = lambdas
        self.l = lambdas[0]
        self.src_vocab_size = src_vocab_size
        super(UnkCountPredictor, self).__init__()
        
    def get_unk_probability(self, posterior):
        """Always returns 0 (= log 1) except for the first time """
        if self.n_consumed == 0:
            return self.max_prob
        return 0.0
    
    def predict_next(self):
        """Set score for EOS to the number of consumed words """
        if self.n_consumed == 0:
            return {utils.EOS_ID : self.unk_prob}
        if self.n_unk < self.max_prob_idx:
            return {utils.EOS_ID : self.unk_prob - self.max_prob}
        return {utils.UNK_ID : self.unk_prob - self.consumed_prob}
    
    def initialize(self, src_sentence):
        """Count UNKs in ``src_sentence`` and reset counters.
        
        Args:
            src_sentence (list): Count UNKs in this list
        """
        src_n_unk = len([w for w in src_sentence if w == utils.UNK_ID 
                                                    or w > self.src_vocab_size])
        self.l = self.lambdas[min(len(self.lambdas)-1, src_n_unk)]
        self.n_consumed = 0
        self.n_unk = 0
        self.unk_prob = self._get_poisson_prob(1)
        # Mode at lambda is the maximum of the poisson function
        self.max_prob_idx = int(self.l)
        self.max_prob = self._get_poisson_prob(self.max_prob_idx)
        ceil_prob = self._get_poisson_prob(self.max_prob_idx + 1)
        if ceil_prob > self.max_prob:
            self.max_prob = ceil_prob
            self.max_prob_idx = self.max_prob_idx + 1
        self.consumed_prob = self.max_prob

    def _get_poisson_prob(self, n):
        """Get the log of the poisson probability for n events. """
        return n * np.log(self.l) - self.l - sum([np.log(i+1) for i in range(n)])
    
    def consume(self, word):
        """Increases unk counter by one if ``word`` is unk.
        
        Args:
            word (int): Increase counter if ``word`` is UNK
        """
        self.n_consumed += 1
        if word == utils.UNK_ID:
            if self.n_unk >= self.max_prob_idx:
                self.consumed_prob = self.unk_prob
            self.n_unk += 1
            self.unk_prob = self._get_poisson_prob(self.n_unk+1)
    
    def get_state(self):
        """Returns the number of consumed words """
        return self.n_unk,self.n_consumed,self.unk_prob,self.consumed_prob
    
    def set_state(self, state):
        """Set the number of consumed words """
        self.n_unk,self.n_consumed,self.unk_prob,self.consumed_prob = state

    def is_equal(self, state1, state2):
        """Returns true if the state is the same"""
        return state1 == state2

    
class NgramizePredictor(Predictor):
    """This wrapper extracts n-gram posteriors from a predictor which
    does not depend on the particular argument of `consume()`. In that
    case, we can build a lookup mechanism for all possible n-grams in
    a single forward pass through the predictor search space: We record
    all posteriors (predict_next() return values) of the slave
    predictor during a greedy pass in `initialize()`. The wrapper
    predictor state is the current n-gram history. We use the 
    (semiring) sum over all possible positions of the current n-gram
    history in the recorded slave predictor posteriors to form the
    n-gram scores returned by this predictor.

    Note that this wrapper does not work correctly if the slave
    predictor feeds back the selected token in the history, ie. depends
    on the particular token which is provided via `consume()`.

    TODO: Make this wrapper work with slaves which return dicts.
    """
    
    def __init__(self, min_order, max_order, max_len_factor, slave_predictor):
        """Creates a new ngramize wrapper predictor.
        
        Args:
            min_order (int): Minimum n-gram order
            max_order (int): Maximum n-gram order
            max_len_factor (int): Stop the forward pass through the 
                                  slave predictor after src_length
                                  times this factor
            slave_predictor (Predictor): Instance of the predictor which
                                         uses the source sentences in
                                         ``src_test``

        Raises:
            AttributeError if order is not positive.
        """
        super(NgramizePredictor, self).__init__()
        if max_order < 1:
             raise AttributeError("max_ngram_order must be positive.")
        if min_order > max_order:
             raise AttributeError("min_ngram_order greater than max_order.")
        self.slave_predictor = slave_predictor
        self.max_history_length = max_order - 1
        self.min_order = max(1, min_order)
        self.max_len_factor = max_len_factor
    
    def initialize(self, src_sentence):
        """Runs greedy decoding on the slave predictor to populate
        self.scores and self.unk_scores, resets the history.
        """
        self.slave_predictor.initialize(src_sentence)
        self.scores = []
        self.unk_scores = []
        trg_word = -1
        max_len = self.max_len_factor * len(src_sentence)
        l = 0
        while trg_word != utils.EOS_ID and l <= max_len:
            posterior = self.slave_predictor.predict_next()
            trg_word = utils.argmax(posterior)
            self.scores.append(posterior)
            self.unk_scores.append(self.slave_predictor.get_unk_probability(
                posterior))
            self.slave_predictor.consume(utils.UNK_ID)
            l += 1
        logging.debug("ngramize uses %d time steps." % l)
        self.history = []
        self.cur_unk_score = utils.NEG_INF
    
    def initialize_heuristic(self, src_sentence):
        """Pass through to slave predictor """
        logging.warning("ngramize does not support predictor heuristics")
        self.slave_predictor.initialize_heuristic(src_sentence)
    
    def predict_next(self):
        """Looks up ngram scores via self.scores. """
        cur_hist_length = len(self.history)
        this_scores = [[] for _ in range(cur_hist_length+1)]
        this_unk_scores = [[] for _ in range(cur_hist_length+1)]
        for pos in range(len(self.scores)):
            this_scores[0].append(self.scores[pos])
            this_unk_scores[0].append(self.unk_scores[pos])
            acc = 0.0
            for order, word in enumerate(self.history):
                if pos + order + 1 >= len(self.scores):
                    break
                acc += utils.common_get(
                    self.scores[pos + order], word, 
                    self.unk_scores[pos + order])
                this_scores[order+1].append(acc + self.scores[pos + order + 1])
                this_unk_scores[order+1].append(
                    acc + self.unk_scores[pos + order + 1])
        combined_scores = []
        combined_unk_scores = []
        for order, (scores, unk_scores) in enumerate(zip(this_scores, 
                                                         this_unk_scores)):
            if scores and order + 1 >= self.min_order:
                score_matrix = np.vstack(scores)
                combined_scores.append(logsumexp(score_matrix, axis=0))
                combined_unk_scores.append(utils.log_sum(unk_scores))
        if not combined_scores:
            self.cur_unk_score = 0.0
            return {}
        self.cur_unk_score = sum(combined_unk_scores)
        return sum(combined_scores)
        
    def get_unk_probability(self, posterior):
        return self.cur_unk_score
    
    def consume(self, word):
        """Pass through to slave predictor """
        if self.max_history_length > 0:
            self.history.append(word)
            self.history = self.history[-self.max_history_length:]
    
    def get_state(self):
        """State is the current n-gram history. """
        return self.history, self.cur_unk_score
    
    def set_state(self, state):
        """State is the current n-gram history. """
        self.history, self.cur_unk_score = state

    def set_current_sen_id(self, cur_sen_id):
        """We need to override this method to propagate current\_
        sentence_id to the slave predictor
        """
        super(NgramizePredictor, self).set_current_sen_id(cur_sen_id)
        self.slave_predictor.set_current_sen_id(cur_sen_id)
    
    def is_equal(self, state1, state2):
        """Pass through to slave predictor """
        return state1 == state2
        

