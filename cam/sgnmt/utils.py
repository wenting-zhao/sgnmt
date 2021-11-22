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

"""This file contains common basic functionality which can be used from
anywhere. This includes the definition of reserved word indices, some
mathematical functions, and helper functions to deal with the small
quirks Python sometimes has.
"""

from abc import abstractmethod
import numpy
import operator
from scipy.special import logsumexp
from subprocess import call
from shutil import copyfile
import logging
import os
import sys

try:
    import pywrapfst as fst
except ImportError:
    try:
        import openfst_python as fst
    except ImportError:
        pass # Deal with it in decode.py

# Reserved IDs
GO_ID = 1
"""Reserved word ID for the start-of-sentence symbol. """


EOS_ID = 2
"""Reserved word ID for the end-of-sentence symbol. """


UNK_ID = 0
"""Reserved word ID for the unknown word (UNK). """


NOTAPPLICABLE_ID = 3
"""Reserved word ID which is currently not used. """


NEG_INF = float("-inf")


INF = float("inf")


EPS_P = 0.00001


def switch_to_fairseq_indexing():
    """Calling this method overrides the global definitions of the 
    reserved  word ids ``GO_ID``, ``EOS_ID``, and ``UNK_ID``
    with the fairseq indexing scheme. 
    """
    global GO_ID
    global EOS_ID
    global UNK_ID
    GO_ID = 0
    EOS_ID = 2
    UNK_ID = 3


def switch_to_t2t_indexing():
    """Calling this method overrides the global definitions of the 
    reserved  word ids ``GO_ID``, ``EOS_ID``, and ``UNK_ID``
    with the tensor2tensor indexing scheme. This scheme is used in all
    t2t models. 
    """
    global GO_ID
    global EOS_ID
    global UNK_ID
    GO_ID = 2 # Usually not used
    EOS_ID = 1
    UNK_ID = 3 # Don't rely on this: UNK not standardized in T2T


# Log summation


def log_sum_tropical_semiring(vals):
    """Approximates summation in log space with the max.
    
    Args:
        vals  (set): List or set of numerical values
    """
    return max(vals)


def log_sum_log_semiring(vals):
    """Uses the ``logsumexp`` function in scipy to calculate the log of
    the sum of a set of log values.
    
    Args:
        vals  (set): List or set of numerical values
    """
    return logsumexp(numpy.asarray([val for val in vals]))


log_sum = log_sum_log_semiring
"""Defines which log summation function to use. """


def oov_to_unk(seq, vocab_size, unk_idx=None):
    if unk_idx is None:
        unk_idx = UNK_ID
    return [x if x < vocab_size else unk_idx for x in seq]

# Maximum functions

def argmax_n(arr, n):
    """Get indices of the ``n`` maximum entries in ``arr``. The 
    parameter ``arr`` can be a dictionary. The returned index set is 
    not guaranteed to be sorted.
    
    Args:
        arr (list,array,dict):  Set of numerical values
        n  (int):  Number of values to retrieve
    
    Returns:
        List of indices or keys of the ``n`` maximum entries in ``arr``
    """
    if isinstance(arr, dict):
        return sorted(arr, key=arr.get, reverse=True)[:n]
    elif len(arr) <= n:
        return range(n)
    else:
        return numpy.argpartition(arr, -n)[-n:]


def argmax(arr):
    """Get the index of the maximum entry in ``arr``. The parameter can
    be a dictionary.
    
    Args:
        arr (list,array,dict):  Set of numerical values
    
    Returns:
        Index or key of the maximum entry in ``arr``
    """
    if isinstance(arr, dict):
        return max(arr.items(), key=operator.itemgetter(1))[0]
    else:
        return numpy.argmax(arr)


# Functions for common access to numpy arrays, lists, and dicts
    

def common_viewkeys(obj):
    """Can be used to iterate over the keys or indices of a mapping.
    Works with numpy arrays, lists, and dicts. Code taken from
    http://stackoverflow.com/questions/12325608/iterate-over-a-dict-or-list-in-python
    """
    if isinstance(obj, dict):
        return obj.keys()
    else:
        return range(len(obj))


def common_iterable(obj):
    """Can be used to iterate over the key-value pairs of a mapping.
    Works with numpy arrays, lists, and dicts. Code taken from
    http://stackoverflow.com/questions/12325608/iterate-over-a-dict-or-list-in-python
    """
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield key, value
    else:
        for index, value in enumerate(obj):
            yield index, value


def common_get(obj, key, default):
    """Can be used to access an element via the index or key.
    Works with numpy arrays, lists, and dicts.
    
    Args:
        ``obj`` (list,array,dict):  Mapping
        ``key`` (int): Index or key of the element to retrieve
        ``default`` (object): Default return value if ``key`` not found
    
    Returns:
        ``obj[key]`` if ``key`` in ``obj``, otherwise ``default``
    """
    if isinstance(obj, dict):
        return obj.get(key, default)
    else:
        return obj[key] if key < len(obj) else default


def common_contains(obj, key):
    """Checks the existence of a key or index in a mapping.
    Works with numpy arrays, lists, and dicts.
    
    Args:
        ``obj`` (list,array,dict):  Mapping
        ``key`` (int): Index or key of the element to retrieve
    
    Returns:
        ``True`` if ``key`` in ``obj``, otherwise ``False``
    """
    if isinstance(obj, dict):
        return key in obj
    else:
        return key < len(obj)


# FST utilities


TMP_FILENAME = '/tmp/sgnmt.%s.fst' % os.getpid()
"""Temporary file name to use if an FST file is zipped. """


def w2f(fstweight):
    """Converts an arc weight to float """
    return float(str(fstweight))


def load_fst(path):
    """Loads a FST from the file system using PyFSTs ``read()`` method.
    GZipped format is also supported. The arc type must be standard
    or log, otherwise PyFST cannot load them.
    
    Args:
        path (string):  Path to the FST file to load
    Returns:
        fst. PyFST FST object or ``None`` if FST could not be read
    """
    try:
        if path[-3:].lower() == ".gz":
            copyfile(path, "%s.gz" % TMP_FILENAME)
            call(["gunzip", "%s.gz" % TMP_FILENAME])
            ret = fst.Fst.read(TMP_FILENAME)
            os.remove(TMP_FILENAME)
        else: # Fst not zipped
            ret = fst.Fst.read(path)
        logging.debug("Read fst from %s" % path)
        return ret
    except Exception as e:
        logging.error("%s error reading fst from %s: %s" %
            (sys.exc_info()[1], path, e))
    return None


# Miscellaneous


def get_path(tmpl, sub = 1):
    """Replaces the %d placeholder in ``tmpl`` with ``sub``. If ``tmpl``
    does not contain %d, return ``tmpl`` unmodified.
    
    Args:
        tmpl (string): Path, potentially with %d placeholder
        sub (int): Substitution for %d
    
    Returns:
        string. ``tmpl`` with %d replaced with ``sub`` if present
    """
    try:
        return tmpl % sub
    except TypeError:
        pass
    return tmpl


def split_comma(s, func=None):
    """Splits a string at commas and removes blanks."""
    if not s:
        return []
    parts = s.split(",")
    if func is None:
        return [el.strip() for el in parts]
    return [func(el.strip()) for el in parts]


MESSAGE_TYPE_DEFAULT = 1
"""Default message type for observer messages """


MESSAGE_TYPE_POSTERIOR = 2
"""This message is sent by the decoder after ``apply_predictors`` was
called. The message includes the new posterior distribution and the
score breakdown. 
"""


MESSAGE_TYPE_FULL_HYPO = 3
"""This message type is used by the decoder when a new complete 
hypothesis was found. Note that this is not necessarily the best hypo
so far, it is just the latest hypo found which ends with EOS.
"""


class Observer(object):
    """Super class for classes which observe (GoF design patten) other
    classes.
    """
    
    @abstractmethod
    def notify(self, message, message_type = MESSAGE_TYPE_DEFAULT):
        """Get a notification from an observed object.
        
        Args:
            message (object): the message sent by observed object
            message_type (int): The type of the message. One of the
                                ``MESSAGE_TYPE_*`` variables
        """
        raise NotImplementedError
    

class Observable(object):
    """For the GoF design pattern observer """
    
    def __init__(self):
        """Initializes the list of observers with an empty list """
        self.observers = []
    
    def add_observer(self, observer):
        """Add a new observer which is notified when this class fires
        a notification
        
        Args:
            observer (Observer): the observer class to add
        """
        self.observers.append(observer)
    
    def notify_observers(self, message, message_type = MESSAGE_TYPE_DEFAULT):
        """Sends the given message to all registered observers.
        
        Args:
            message (object): The message to send
            message_type (int): The type of the message. One of the
                                ``MESSAGE_TYPE_*`` variables
        """
        for observer in self.observers:
            observer.notify(message, message_type)
    
    
