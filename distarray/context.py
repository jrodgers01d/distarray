# encoding: utf-8
#----------------------------------------------------------------------------
#  Copyright (C) 2008-2014, IPython Development Team and Enthought, Inc.
#  Distributed under the terms of the BSD License.  See COPYING.rst.
#----------------------------------------------------------------------------


import uuid
from distarray.externals import six
import collections

from IPython.parallel import Client
import numpy

from distarray.client import DistArray


class Context(object):
    '''
    Context objects manage the setup and communication of the worker processes
    for DistArray objects.  A DistArray object has a context, and contexts have
    an MPI intracommunicator that they use to communicate with worker
    processes.

    Typically there is just one context object that uses all processes,
    although it is possible to have more than one context with a different
    selection of engines.

    '''

    def __init__(self, client=None, targets=None):
        self.client = client if client is not None else Client()
        self.view = self.client[:]

        all_targets = self.view.targets
        if targets is None:
            self.targets = all_targets
        else:
            self.targets = []
            for target in targets:
                if target not in all_targets:
                    raise ValueError("Engine with id %r not registered" % target)
                else:
                    self.targets.append(target)

        # FIXME: IPython bug #4296: This doesn't work under Python 3
        #with self.view.sync_imports():
        #    import distarray
        self.view.execute("import distarray.local; "
                          "import distarray.mpiutils; "
                          "import numpy")

        self._setup_key_context()
        self._make_intracomm()
        self._set_engine_rank_mapping()

    def __del__(self):
        """ Clean up keys we have put on the engines. """
        # There is an order-of-ops problem here.
        # We sometimes call Client.close() explicitly, while leaving
        # this call to run when garbage is collected.
        # BUT, once we call close(), we cannot clean up the keys anymore.
        #return    # leak!
        self._cleanup_keys()

    def _cleanup_keys(self):
        """ Delete all the keys we have created from all the engines. """
        print ('cleanup keys...')
        # Cleanup normal keys.
        self.purge_keys()
        # And the comm_key.
        if hasattr(self, '_comm_key'):
            print 'deleting comm key', self._comm_key
            self.delete_key(self._comm_key)
            self._comm_key = None

    def _set_engine_rank_mapping(self):
        # The MPI intracomm referred to by self._comm_key may have a different
        # mapping between IPython engines and MPI ranks than COMM_PRIVATE.  We
        # reorder self.targets so self.targets[i] is the IPython engine ID that
        # corresponds to MPI intracomm rank i.
        rank = self._generate_key()
        self.view.execute(
                '%s = %s.Get_rank()' % (rank, self._comm_key),
                block=True, targets=self.targets)

        # mapping target -> rank, rank -> target.
        target_to_rank = self.view.pull(rank, targets=self.targets).get_dict()
        rank_to_target = {v: k for (k, v) in target_to_rank.items()}
#         # Probably wont need this...
#         self.delete_key(rank)

        # ensure consistency
        assert set(self.targets) == set(target_to_rank)
        assert set(range(len(self.targets))) == set(rank_to_target)

        # reorder self.targets so that the targets are in MPI rank order for
        # the intracomm.
        self.targets = [rank_to_target[i] for i in range(len(rank_to_target))]

    def _make_intracomm(self):
        def get_rank():
            from distarray.mpiutils import COMM_PRIVATE
            return COMM_PRIVATE.Get_rank()

        # get a mapping of IPython engine ID to MPI rank
        rank_map = self.view.apply_async(get_rank).get_dict()
        ranks = [ rank_map[engine] for engine in self.targets ]

        # self.view's engines must encompass all ranks in the MPI communicator,
        # i.e., everything in rank_map.values().
        def get_size():
            from distarray.mpiutils import COMM_PRIVATE
            return COMM_PRIVATE.Get_size()

        comm_size = self.view.apply_async(get_size).get()[0]
        if set(rank_map.values()) != set(range(comm_size)):
            raise ValueError('Engines in view must encompass all MPI ranks.')

        # create a new communicator with the subset of engines note that
        # MPI_Comm_create must be called on all engines, not just those
        # involved in the new communicator.
        # Apply a prefix to this key name so it is not wrongly removed.
        self._comm_key = self._generate_key(prefix='comm')
        print 'comm key:', self._comm_key
        self.view.execute(
            '%s = distarray.mpiutils.create_comm_with_list(%s)' % (self._comm_key, ranks),
            block=True
        )

    # Key management routines:

    def _setup_key_context(self):
        """ Generate a unique string for this context.

        This will be included in the names of all keys we create.
        This prefix allows us to delete only keys from this context.
        """
        # Full length seems excessively verbose so use 16 characters.
        uid = uuid.uuid4()
        self.key_context = uid.hex[:16]

    def _key_base(self, prefix=None):
        """ Get the base name for all keys.
        
        The prefix is used to give the comm_key a name that will not be
        inadvertently deleted.
        """
        base = '__distarray_'
        if prefix is not None:
            header = base + '_' + prefix
        else:
            header = base
        return header

    def _key_header(self, prefix=None):
        """ Generate a header for a key name for this context.
        
        The key name will start as: '__distarray__[prefix]_[key_context]_'.
        The prefix is used to give the comm_key a name that will not be
        inadvertently deleted.
        """
        header = self._key_base(prefix) + '_' + self.key_context
        return header

    def _generate_key(self, prefix=None):
        """ Generate a unique key name, including an identifier for this context. """
        uid = uuid.uuid4()
        key = self._key_header(prefix) + '_' + uid.hex
        #print 'generated key:', key
        return key

    def _key_and_push(self, *values):
        keys = [self._generate_key() for value in values]
        self._push(dict(zip(keys, values)))
        return tuple(keys)

    def delete_key(self, key):
        """ Delete the specific key from all the engines. """
        cmd = 'del %s' % key
        self._execute(cmd)

    def purge_keys(self, prefix=None, all_contexts=False):
        """ Delete keys that this context created from all the engines. """
        if all_contexts:
            # Delete distarray keys from all contexts.
            # But we need to skip our comm key. 
            header = self._key_base(prefix)
            comm_header = self._key_header('comm')
            print 'purge key header:', header
            print 'our comm header:', comm_header 
            cmd = """for k in globals().keys():
                         if k.startswith('%s') and not k.startswith('%s'):
                             del globals()[k]""" % (header, comm_header)
        else:
            # Delete keys only from this context.
            header = self._key_header(prefix)
            print 'purge key header:', header
            cmd = """for k in globals().keys():
                         if k.startswith('%s'):
                             del globals()[k]""" % (header)
        print 'cmd:', cmd
        self._execute(cmd)

    def dump_keys(self, prefix=None, all_contexts=False):
        """ Return a list of all key names present on the engines.

        The list is a list of tuples (key name, list of targets),
        and is sorted by key name. This is intended to be convenient
        and readable to print out.
        """
        if all_contexts:
            # Dump distarray keys from all contexts.
            header = self._key_base(prefix)
        else:
            # Dump keys only from this context.
            header = self._key_header(prefix)
        print 'dump key header:', header
        # Get the keys from all the engines.
        dump_key = self._generate_key()
        cmd = '%s = [k for k in globals().keys() if k.startswith("%s")]' % (
            dump_key, header)
        print 'cmd:', cmd
        self._execute(cmd)
        keylists = self._pull(dump_key)
        # The values returned by the engines are a nested list,
        # the outer per engine, and the inner listing each key name.
        # Convert to dict with key=key, value=list of targets.
        engine_keys = {}
        for iengine, keylist in enumerate(keylists):
            for key in keylist:
                if key not in engine_keys:
                    engine_keys[key] = []
                engine_keys[key].append(self.targets[iengine])
        # Convert to sorted list of tuples (key name, list of targets).
        keylist = []
        for key in sorted(engine_keys.keys()):
            targets = engine_keys[key]
            keylist.append((key, targets))
        return keylist

    # End of key management routines.

    def _execute(self, lines):
        return self.view.execute(lines,targets=self.targets,block=True)

    def _push(self, d):
        return self.view.push(d,targets=self.targets,block=True)

    def _pull(self, k):
        return self.view.pull(k,targets=self.targets,block=True)

    def _execute0(self, lines):
        return self.view.execute(lines,targets=self.targets[0],block=True)

    def _push0(self, d):
        return self.view.push(d,targets=self.targets[0],block=True)

    def _pull0(self, k):
        return self.view.pull(k,targets=self.targets[0],block=True)

    def zeros(self, shape, dtype=float, dist={0:'b'}, grid_shape=None):
        keys = self._key_and_push(shape, dtype, dist, grid_shape)
        da_key = self._generate_key()
        subs = (da_key,) + keys + (self._comm_key,)
        self._execute(
            '%s = distarray.local.zeros(%s, %s, %s, %s, %s)' % subs
        )
        return DistArray(da_key, self)

    def ones(self, shape, dtype=float, dist={0:'b'}, grid_shape=None):
        keys = self._key_and_push(shape, dtype, dist, grid_shape)
        da_key = self._generate_key()
        subs = (da_key,) + keys + (self._comm_key,)
        self._execute(
            '%s = distarray.local.ones(%s, %s, %s, %s, %s)' % subs
        )
        return DistArray(da_key, self)

    def empty(self, shape, dtype=float, dist={0:'b'}, grid_shape=None):
        keys = self._key_and_push(shape, dtype, dist, grid_shape)
        da_key = self._generate_key()
        subs = (da_key,) + keys + (self._comm_key,)
        self._execute(
            '%s = distarray.local.empty(%s, %s, %s, %s, %s)' % subs
        )
        return DistArray(da_key, self)

    def save(self, name, da):
        """
        Save a distributed array to files in the ``.dnpy`` format.

        Parameters
        ----------
        name : str or list of str
            If a str, this is used as the prefix for the filename used by each
            engine.  Each engine will save a file named
            ``<name>_<rank>.dnpy``.
            If a list of str, each engine will use the name at the index
            corresponding to its rank.  An exception is raised if the
            length of this list is not the same as the communicator's size.
        da : DistArray
            Array to save to files.

        """
        if isinstance(name, six.string_types):
            subs = self._key_and_push(name) + (da.key, da.key)
            self._execute(
                'distarray.local.save(%s + "_" + str(%s.comm_rank) + ".dnpy", %s)' % subs
            )
        elif isinstance(name, collections.Iterable):
            if len(name) != len(self.targets):
                errmsg = "`name` must be the same length as `self.targets`."
                raise TypeError(errmsg)
            subs = self._key_and_push(name) + (da.key, da.key)
            self._execute(
                'distarray.local.save(%s[%s.comm_rank], %s)' % subs
            )
        else:
            errmsg = "`name` must be a string or a list."
            raise TypeError(errmsg)


    def load(self, name):
        """
        Load a distributed array from ``.dnpy`` files.

        Parameters
        ----------
        name : str or list of str
            If a str, this is used as the prefix for the filename used by each
            engine.  Each engine will load a file named
            ``<name>_<rank>.dnpy``.
            If a list of str, each engine will use the name at the index
            corresponding to its rank.  An exception is raised if the length of
            this list is not the same as the communicator's size.

        Returns
        -------
        result : DistArray
            A DistArray encapsulating the file loaded on each engine.

        """
        da_key = self._generate_key()
        subs = (da_key, name, self._comm_key)

        if isinstance(name, six.string_types):
            subs = (da_key,) + self._key_and_push(name) + (self._comm_key,
                    self._comm_key)
            self._execute(
                '%s = distarray.local.load(%s + "_" + str(%s.Get_rank()) + ".dnpy", %s)' % subs
            )
        elif isinstance(name, collections.Iterable):
            if len(name) != len(self.targets):
                errmsg = "`name` must be the same length as `self.targets`."
                raise TypeError(errmsg)
            subs = (da_key,) + self._key_and_push(name) + (self._comm_key,
                    self._comm_key)
            self._execute(
                '%s = distarray.local.load(%s[%s.Get_rank()], %s)' % subs
            )
        else:
            errmsg = "`name` must be a string or a list."
            raise TypeError(errmsg)

        return DistArray(da_key, self)

    def save_hdf5(self, filename, da, key='buffer', mode='a'):
        """
        Save a DistArray to a dataset in an ``.hdf5`` file.

        Parameters
        ----------
        filename : str
            Name of file to write to.
        da : DistArray
            Array to save to a file.
        key : str, optional
            The identifier for the group to save the DistArray to (the default
            is 'buffer').
        mode : optional, {'w', 'w-', 'a'}, default 'a'
            ``'w'``
                Create file, truncate if exists
            ``'w-'``
                Create file, fail if exists
            ``'a'``
                Read/write if exists, create otherwise (default)

        """
        try:
            # this is just an early check,
            # h5py isn't necessary until the local call on the engines
            import h5py
        except ImportError:
            errmsg = "An MPI-enabled h5py must be available to use save_hdf5."
            raise ImportError(errmsg)

        subs = (self._key_and_push(filename) + (da.key,) +
                self._key_and_push(key, mode))
        self._execute(
            'distarray.local.save_hdf5(%s, %s, %s, %s)' % subs
        )

    def load_npy(self, filename, dim_data_per_process, grid_shape=None):
        """
        Load a DistArray from a dataset in a ``.npy`` file.

        Parameters
        ----------
        filename : str
            Filename to load.
        dim_data_per_process : iterable of tuples of dict
            A "dim_data" data structure for every process.  Described here:
            https://github.com/enthought/distributed-array-protocol
        grid_shape : tuple of int, optional
            Shape of process grid.

        Returns
        -------
        result : DistArray
            A DistArray encapsulating the file loaded.

        """
        if len(self.targets) != len(dim_data_per_process):
            errmsg = "`dim_data_per_process` must contain a dim_data for every process."
            raise TypeError(errmsg)

        da_key = self._generate_key()
        subs = ((da_key,) + self._key_and_push(filename, dim_data_per_process) +
                (self._comm_key,) + (self._comm_key,))

        self._execute(
            '%s = distarray.local.load_npy(%s, %s[%s.Get_rank()], %s)' % subs
        )

        return DistArray(da_key, self)

    def load_hdf5(self, filename, dim_data_per_process, key='buffer',
                  grid_shape=None):
        """
        Load a DistArray from a dataset in an ``.hdf5`` file.

        Parameters
        ----------
        filename : str
            Filename to load.
        dim_data_per_process : iterable of tuples of dict
            A "dim_data" data structure for every process.  Described here:
            https://github.com/enthought/distributed-array-protocol
        key : str, optional
            The identifier for the group to load the DistArray from (the
            default is 'buffer').
        grid_shape : tuple of int, optional
            Shape of process grid.

        Returns
        -------
        result : DistArray
            A DistArray encapsulating the file loaded.

        """
        try:
            import h5py
        except ImportError:
            errmsg = "An MPI-enabled h5py must be available to use load_hdf5."
            raise ImportError(errmsg)

        if len(self.targets) != len(dim_data_per_process):
            errmsg = "`dim_data_per_process` must contain a dim_data for every process."
            raise TypeError(errmsg)

        da_key = self._generate_key()
        subs = ((da_key,) + self._key_and_push(filename, dim_data_per_process) +
                (self._comm_key,) + self._key_and_push(key) + (self._comm_key,))

        self._execute(
            '%s = distarray.local.load_hdf5(%s, %s[%s.Get_rank()], %s, %s)' % subs
        )

        return DistArray(da_key, self)

    def fromndarray(self, arr, dist={0: 'b'}, grid_shape=None):
        """Convert an ndarray to a distarray."""
        out = self.empty(arr.shape, dtype=arr.dtype, dist=dist,
                         grid_shape=grid_shape)
        for index, value in numpy.ndenumerate(arr):
            out[index] = value
        return out

    fromarray = fromndarray

    def fromfunction(self, function, shape, **kwargs):
        func_key = self._generate_key()
        self.view.push_function({func_key:function},targets=self.targets,block=True)
        keys = self._key_and_push(shape, kwargs)
        new_key = self._generate_key()
        subs = (new_key,func_key) + keys
        self._execute('%s = distarray.local.fromfunction(%s,%s,**%s)' % subs)
        return DistArray(new_key, self)
