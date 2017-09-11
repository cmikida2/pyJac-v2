# -*- coding: utf-8 -*-
"""Contains various utility classes for creating loopy arrays
and indexing / mapping
"""


import logging
import loopy as lp
import numpy as np
import copy
from loopy.kernel.data import temp_var_scope as scopes

problem_size = lp.ValueArg('problem_size', dtype=np.int32)
"""
    The problem size variable for non-testing
"""

global_ind = 'j'
"""str: The global initial condition index

This is the string index for the global condition loop in generated kernels
of :module:`rate_subs`
"""


var_name = 'i'
"""str: The inner loop index

This is the string index for the inner loops in generated kernels of
:module:`rate_subs`
"""


default_inds = (global_ind, var_name)
"""str: The default indicies used in main loops of :module:`rate_subs`

This is the string indicies for the main loops for generated kernels in
:module:`rate_subs`
"""

class tree_node(object):

    """
        A node in the :class:`MapStore`'s domain tree.
        Contains a base domain, a list of child domains, and a list of
        variables depending on this domain

    Parameters
    ----------
    owner: :class:`MapStore`
        The owning mapstore, used for iname creation
    parent : :class:`tree_node`
        The parent domain of this tree node
    domain : :class:`creator`
        The domain this :class:`tree_node` represents
    children : list of :class:`tree_node`
        The leaves of this node
    iname : str
        The iname of this :class:`tree_node`
    """

    def __add_to_owner(self, domain):
        assert domain not in self.owner.domain_to_nodes, (
            'Domain {} is already present in the tree!'.format(
                domain.name))
        self.owner.domain_to_nodes[self.domain] = self

    def __init__(self, owner, domain, children=[],
                 parent=None, iname=None):
        self.domain = domain
        self.owner = owner
        try:
            self.children = set(children)
        except:
            self.children = set([children])
        self.parent = parent
        self.transform = None
        self._iname = iname

        self.insn = None
        self.domain_transform = None

        # book keeping
        self.__add_to_owner(domain)
        for child in self.children:
            self.__add_to_owner(child)

    def is_leaf(self):
        return not self.children and self != self.owner.tree

    @property
    def iname(self):
        return self._iname

    @iname.setter
    def iname(self, value):
        if value is None:
            assert self.parent is not None, (
                "Can't set empty (untransformed) iname for root!")
            self._iname = self.parent.iname
        else:
            self._iname = value

    def set_transform(self, iname, insn, domain_transform):
        self.iname = iname
        self.insn = insn
        self.domain_transform = domain_transform

    def add_child(self, domain):
        """
        Adds a child domain (if not already present) to this node

        Parameters
        ----------
        domain : :class:`creator`
            The domain to create a the child node with

        Returns
        -------
        child : :class:`tree_node`
            The newly created tree node
        """

        # check for existing child
        child = next((x for x in self.children if x.domain == domain), None)

        if child is None:
            child = tree_node(self.owner, domain, parent=self)
            self.children.add(child)
            self.__add_to_owner(child)

        return child

    def __repr__(self):
        return ', '.join(['{}'.format(x) for x in
                          (self.domain.name, self.iname, self.insn)])


class domain_transform(object):

    """
    Simple helper class to keep track of transform variables to test for
    equality
    """

    def __init__(self, mapping, affine):
        self.mapping = mapping
        self.affine = affine

    def __eq__(self, other):
        return self.mapping == other.mapping and self.affine == other.affine


class MapStore(object):

    """
    This class manages maps and masks for inputs / outputs in kernels

    Attributes
    ----------

    loopy_opts : :class:`LoopyOptions`
        The loopy options for kernel creation
    use_private_memory : Bool [False]
        If True, use _private_ OpenCL/CUDA/etc. memory for array creation.
        If False, use _global_ memory.
    knl_type : ['map', 'mask']
        The kernel mapping / masking type.  Controls whether this kernel should
        generate maps vs masks and the index ranges
    map_domain : :class:`creator`
        The domain of the iname to use for a mapped kernel
    mask_domain : :class:`creator`
        The domain of the iname to use for a masked kernel
    iname : str
        The loop index to work with
    have_input_map : bool
        If true, the input map domain needs a map for expression
    transformed_domains : set of :class:`tree_node`
        The nodes that have required transforms
    transform_insns : set
        A set of transform instructions generated for this :class:`MapStore`
    raise_on_final : bool
        If true, raise an exception if a variable / domain is added to the
        domain tree after this :class:`MapStore` has been finalized
    """

    def __init__(self, loopy_opts, map_domain, mask_domain, iname='i',
                 raise_on_final=True):
        self.loopy_opts = loopy_opts
        self.use_private_memory = loopy_opts.use_private_memory
        self.knl_type = loopy_opts.knl_type
        self.map_domain = map_domain
        self.mask_domain = mask_domain
        self._check_is_valid_domain(self.map_domain)
        self._check_is_valid_domain(self.mask_domain)
        self.domain_to_nodes = {}
        self.transformed_domains = set()
        self.tree = tree_node(self, self._get_base_domain(), iname=iname)
        self.domain_to_nodes[self._get_base_domain()] = self.tree
        from pytools import UniqueNameGenerator
        self.taken_transform_names = UniqueNameGenerator(set([iname]))
        self.iname = iname
        self.have_input_map = False
        self.raise_on_final = raise_on_final
        self.is_finalized = False

    def _is_map(self):
        """
        Return true if map kernel
        """

        return self.knl_type == 'map'

    @property
    def transform_insns(self):
        return set(val.insn for val in
                   self.transformed_domains if val.insn)

    def _add_input_map(self):
        """
        Adds an input map, and remakes the base domain
        """

        # copy the base map domain
        new_creator_name = self.map_domain.name + '_map'
        new_map_domain = self.map_domain.copy()

        # update new domain
        new_map_domain.name = new_creator_name
        new_map_domain.initializer = \
            np.arange(self.map_domain.initializer.size, dtype=np.int32)

        # change the base of the tree
        new_base = tree_node(self, new_map_domain, iname=self.iname,
                             children=self.tree)

        # and parent

        # to maintain consistency/sanity, we always consider the original tree
        # the 'base', even if the true base is being replaced
        # This will be accounted for in :meth:`finalize`
        self.tree.parent = new_base

        # reset iname
        self.tree.iname = None

        # update domain
        self.map_domain = new_map_domain

        # and finally set tree
        self.domain_to_nodes[new_map_domain] = new_base

        # finally, check the tree's offspring.  If they can be moved to the
        # new base without mapping, do so

        for child in list(self.tree.children):
            if not child.is_leaf():
                mapping, affine = self._get_map_transform(
                    new_map_domain, child.domain)
                if not mapping:
                    # set parent
                    child.parent = new_base
                    # remove from old parent's child list
                    self.tree.children.remove(child)
                    # and add to new parent's child list
                    new_base.children.add(child)

        self.have_input_map = True

    def _create_transform(self, node, transform, affine=None):
        """
        Creates a transform from the :class:`tree_node` node based on
        it's parent and any affine mapping supplied

        Parameters
        ----------
        node : :class:`tree_node`
            The node to create a transform for
        transform : :class:`domain_transform`
            The domain transform to store the base iname in
        affine : int or dict
            An integer or dictionary offset

        Returns
        -------
        new_iname : str
            The iname created for this transform
        transform_insn : str or None
            The loopy transform instruction to use.  If None, this is an
            affine transformation that doesn't require a separate instruction
        """

        assert node.parent is not None, (
            'Cannot create a transform for node'
            ' {} without parent'.format(node.domain.name))

        assert node.parent.iname, (
            'Cannot create a transform starting from parent node'
            ' {}, as it has no assigned iname'.format(node.parent.domain.name))

        # add the transformed inames, instruction and map
        new_iname = self._get_transform_iname(self.iname)

        # and use the parent's "iname" (i.e. with affine mapping)
        # to generate the transform
        transform_insn = self.generate_transform_instruction(
            node.parent.iname, new_iname, map_arr=node.domain.name,
            affine=affine
        )

        if affine:
            # store this as the new iname instead of issuing a new instruction
            new_iname = transform_insn
            transform_insn = None

        return new_iname, transform_insn

    def _get_transform_iname(self, iname):
        """Returns a new iname"""
        return self.taken_transform_names(iname)

    def _get_mask_transform(self, domain, new_domain):
        """
        Get the appropriate map transform between two given domains.
        Most likely, this will be a numpy array, but it may be an affine
        mapping

        Parameters
        ----------
        domain : :class:`creator`
            The domain to map
        new_domain: :class:`creator`
            The domain to map to

        Returns
        -------
        None if domains are equivalent
        Affine `str` map if an affine transform is possible
        :class:`creator` if a more complex map is required
        """

        try:
            dcheck = domain.initializer
        except AttributeError:
            dcheck = domain
        try:
            ncheck = new_domain.initializer
        except AttributeError:
            ncheck = new_domain

        # check equal
        if np.array_equal(dcheck, ncheck):
            return None, None

        # check for affine
        dset = np.where(dcheck != -1)[0]
        nset = np.where(ncheck != -1)[0]

        # must be same size for affine
        if dset.size == nset.size:
            # in order to be an affine mask transform, the set values should be
            # an affine transform
            diffs = nset - dset
            affine = diffs[0]
            if np.all(diffs == affine):
                # additionally, the affine mapped values should match the
                # original ones
                if np.array_equal(ncheck[nset], dcheck[dset]):
                    return new_domain, affine

        return new_domain, None

    def _get_map_transform(self, domain, new_domain):
        """
        Get the appropriate map transform between two given domains.
        Most likely, this will be a numpy array, but it may be an affine
        mapping

        Parameters
        ----------
        domain : :class:`creator`
            The domain to map
        new_domain: :class:`creator`
            The domain to map to

        Returns
        -------
        new_domain : :class:`creator`
        If not None, this is the mapping that must be used
            - None if domains are equivalent
            - `str` map if an affine transform is possible
            - :class:`creator` if a more complex map is required
        """

        try:
            dcheck = domain.initializer
        except AttributeError:
            dcheck = domain
        try:
            ncheck = new_domain.initializer
        except AttributeError:
            ncheck = new_domain

        # first, we need to make sure that the domains are the same size,
        # non-sensical otherwise

        if dcheck.shape != ncheck.shape:
            # Can't use affine map on domains of differing sizes
            return new_domain, None

        # check equal
        if np.array_equal(dcheck, ncheck):
            return None, None

        # check for affine map
        if np.all(ncheck - dcheck == (ncheck - dcheck)[0]):
            return new_domain, (ncheck - dcheck)[0]

        # finally return map
        return new_domain, None

    def _get_transform(self, base, domain):
        return self._get_map_transform(base, domain) if self._is_map()\
            else self._get_mask_transform(base, domain)

    def _check_is_valid_domain(self, domain):
        """Makes sure the domain passed is a valid :class:`creator`"""
        assert domain is not None, 'Invalid domain'
        assert isinstance(domain, creator), ('Domain'
                                             ' must be of type `creator`')
        assert domain.name is not None, ('Domain must have initialized name')
        assert domain.initializer is not None, (
            'Cannot use non-initialized creator {} as domain!'.format(
                domain.name))

        if not self._is_map():
            # need to check that the maximum value is smaller than the base
            # mask domain size
            assert np.max(domain.initializer) < \
                self.mask_domain.initializer.size, (
                    "Mask entries for domain {} cannot be outside of "
                    "domain size {}".format(domain.name,
                                            self.mask_domain.initializer.size))

    def _is_contiguous(self, domain):
        """Returns true if domain can be expressed with a simple for loop"""
        indicies = domain.initializer
        return indicies[0] + indicies.size - 1 == indicies[-1]

    def _check_create_transform(self, node):
        """
        Checks and creates a transform between the node and the
        parent node if necessary

        Parameters
        ----------
        node : :class:`tree_node`
            The domain to check

        Returns
        -------
        new_iname : str
            The iname created for this transform
        transform_insn : str or None
            The loopy transform instruction to use.  If None, this is an
            affine transformation that doesn't require a separate instruction
        transform : :class:`domain_transform`
            The representation of the transform used, for equality testing
        """

        domain = node.domain
        # check to see if root
        if node.parent is None:
            return None, None, None
        base = node.parent.domain

        # if this node should be treated as a variable,
        # don't create a transform
        if node.is_leaf():
            return None, None, None

        # get mapping
        mapping, affine = self._get_transform(base, domain)

        # if we actually need a mapping
        if mapping is not None:
            dt = domain_transform(mapping, affine)
            # see if this map already exists
            if node in self.transformed_domains:
                if dt == node.domain_transform:
                    return node.iname, node.insn, node.domain_transform

            # need a new map, so add
            iname, insn = self._create_transform(node, dt, affine=affine)
            return iname, insn, dt

        return None, None, None

    def _get_base_domain(self):
        """
        Conviencience method to get domain agnostic of map / mask type
        """
        if self.knl_type == 'map':
            return self.map_domain
        elif self.knl_type == 'mask':
            return self.mask_domain
        else:
            raise NotImplementedError

    def check_and_add_transform(self, variable, domain, iname=None,
                                force_inline=False):
        """
        Check the domain of the variable given against the base domain of this
        kernel.  If not a match, a map / mask instruction will be generated
        as necessary--i.e. if no other variable with the transformed domain has
        already been specified--and the mapping will be stored for string
        creation

        Parameters
        ----------
        variable : :class:`creator` or list thereof
            The NameStore variable(s) to work with
        domain : :class:`creator`
            The domain of the variable to check.
            Note: this must be an initialized creator (i.e. temporary variable)
        iname : str
            The iname to transform.  If not specified, it will default to 'i'
            _or_ the iname for the transform of this domain
        force_inline : bool
            If True, the developed transform (if any) must be expressed as an
            inline transform.  If the transform is not affine, an exception
            will be raised

        Returns
        -------
        transform : :class:`domain_transform`
            The resulting transform, or None if not added
        """

        if self.is_finalized:
            if self.raise_on_final:
                raise Exception(
                    'Cannot add domain {} for variable {} to the tree as this'
                    ' mapstore is finalized, which may invalidate '
                    ' previously calculated transform data'.format(
                        domain.name,
                        variable.name))
            else:
                logging.warn(
                    'Adding domain {} for variable {} to tree after'
                    ' finalization, this should not be used outside of unit'
                    ' testing'.format(domain.name, variable.name))

        # make sure this domain is valid
        self._check_is_valid_domain(domain)

        # check to see if this domain is already in the tree
        try:
            node = self.domain_to_nodes[domain]
            assert node.domain.initializer is not None, (
                "Can't use non-initialized creator {} as a transform domain".
                format(node.domain.name))
        except:
            # add the domain to the base of the tree
            node = self.tree.add_child(domain)

        # add variable to the new tree node
        node.add_child(variable)

    def finalize(self):
        """
        Turns the developed domain tree into transforms and instructions
        so that variables can begin to be created.  Called automatically on
        first use of :meth:`apply_maps`

        Parameters
        ----------
        None

        Returns
        -------
        None

        """

        # need to check the first level to see if we need an input map
        if self._is_map():
            # if it's not a contiguous name, we're forced to take a map
            if not self._is_contiguous(self.map_domain):
                self._add_input_map()
            # otherwise, test if we need one because of a child domain
            if not self.have_input_map:
                base = self.tree.domain
                for child in list(self.tree.children):
                    if not child.is_leaf() and not self.have_input_map:
                        mapping, affine = self._get_map_transform(
                            base, child.domain)
                        if mapping and not affine and base.initializer[0] != 0:
                            # need and input map
                            self._add_input_map()

        # next, we need to create our transforms
        # the goal here is to recursively transverse the tree checking whether
        # a transform is needed, such that any applied maps pick up the
        # right combination of transforms / inames

        base = self.tree if self.tree.parent is None else self.tree.parent
        branches = [[base]]

        while branches:
            # grab the current nodes under consideration
            branch = branches.pop()

            # for each sub domain in this branch
            for node in branch:
                if node != base:
                    # check for transform
                    iname, insn, dt = self._check_create_transform(node)
                    # and update node (empty transform will take the parent's)
                    # iname
                    node.set_transform(iname, insn, dt)

                    if not (iname is None and insn is None and dt is None):
                        # have a transform, add to the variable list
                        self.transformed_domains.add(node)
                    else:
                        # carry the parent's iname
                        node.iname = node.parent.iname

                # and update the branch lists
                branches.append(list(node.children))

        self.is_finalized = True

    def apply_maps(self, variable, *indicies, **kwargs):
        """
        Applies the developed iname mappings to the indicies supplied and
        returns the created loopy Arg/Temporary and the string version

        Parameters
        ----------
        variable : :class:`creator`
            The NameStore variable(s) to work with
        indices : list of str
            The inames to map
        affine : int
            An affine transformation to apply inline to this variable.
        Returns
        -------
        lp_var : :class:`loopy.GlobalArg` or :class:`loopy.TemporaryVariable`
            The generated variable
        lp_str : str
            The string indexed variable
        """

        if not self.is_finalized:
            self.finalize()

        affine = kwargs.pop('affine', None)

        var_affine = 0
        if variable.affine is not None:
            var_affine = variable.affine

        have_affine = var_affine or affine

        def __get_affine(iname):
            if not have_affine:
                return iname
            aff = 0
            if isinstance(affine, dict):
                if iname in affine:
                    aff = affine[iname]
            elif affine is not None:
                aff = affine
            if isinstance(aff, str):
                if var_affine:
                    aff += ' + {}'.format(var_affine)
                return iname + ' + {}'.format(aff)
            elif aff or var_affine:
                aff += var_affine
                return iname + ' {} {}'.format('+' if aff >= 0 else '-',
                                               np.abs(aff))
            return iname

        if variable in self.domain_to_nodes:
            # get the node this belongs to
            node = self.domain_to_nodes[variable]
        else:
            # ensure that any input map is picked up
            node = self.tree

        if have_affine and len(indicies) != 1 and not isinstance(affine, dict):
            raise Exception("Can't apply affine transformation to indicies, {}"
                            " as the index to apply to cannot be"
                            " determined".format(','.join(indicies)))

        # pick up any mappings
        indicies = tuple(x if x != self.iname else
                         (node.iname if node.is_leaf() or node == self.tree
                          else node.parent.iname)
                         for x in indicies)

        # return affine mapping
        return variable(*tuple(__get_affine(i) for i in indicies),
                        use_private_memory=self.use_private_memory, **kwargs)

    def copy(self):
        return copy.deepcopy(self)

    def generate_transform_instruction(self, oldname, newname, map_arr,
                                       affine='',
                                       force_inline=False):
        """
        Generates a loopy instruction that maps oldname -> newname via the
        mapping array (non-affine), or a simple affine transformation

        Parameters
        ----------
        oldname : str
            The old index to map from
        newname : str
            The new temporary variable to map to
        map_arr : str
            The array that holds the mappings
        affine : int, optional
            An optional affine mapping term that may be passed in
        force_inline : bool, optional
            If true, and affine simply return an inline transform rather than
            a separate instruction

        Returns
        -------
        map_inst : str
            A string to be used as a `loopy.Instruction`
        """

        try:
            affine = ' + ' + str(int(affine))
            return oldname + affine
        except:
            if affine is None:
                affine = ''
            pass

        return '<> {newname} = {mapper}[{oldname}]{affine}'.format(
            newname=newname,
            mapper=map_arr,
            oldname=oldname,
            affine=affine)

    def get_iname_domain(self):
        """
        Get the final iname / domain for kernel generation

        Returns
        -------
        iname_tup : tuple of ('iname', 'range')
            The iname and range string to be fed to loopy
        """

        base = self._get_base_domain()
        fmt_str = '{start} <= {ind} <= {end}'

        if self._is_map():
            return (self.iname, fmt_str.format(
                    ind=self.iname,
                    start=base.initializer[0],
                    end=base.initializer[-1]))
        else:
            return (self.iname, fmt_str.format(
                    ind=self.iname,
                    start=0,
                    end=base.initializer.size - 1))


class creator(object):

    """
    The generic namestore interface, allowing easy access to
    loopy object creation, mapping, masking, etc.
    """

    def __init__(self, name, dtype, shape, order,
                 initializer=None, scope=scopes.GLOBAL,
                 fixed_indicies=None, is_temporary=False, affine=None,
                 is_input_or_output=False):
        """
        Initializes the creator object

        Parameters
        ----------
        name : str
            The name of the loopy array to create
        dtype : :class:`numpy.dtype`
            The dtype of the array
        shape : tuple of (int, str)
            The shape of the array to create, parseable by loopy
        initializer : :class:`numpy.ndarray`
            If specified, the initializer of this array
        scope : :class:`loopy.temp_var_scope`
            The scope of an initialized loopy array
        fixed_indicies : list of tuple
            If supplied, a list of index number, fixed values that
            specify indicies (e.g. for the Temperature/Phi array)
        order : ['C', 'F']
            The row/column-major data format to use in storage
        is_temporary : bool
            If true, this should be a temporary variable
        affine : int
            If supplied, this represents an offset that should be applied to
            the creator upon indexing
        is_input_or_output : bool [False]
            If true, this creator is an input or output variable for pyJac.
            Hence, it should not use private memory, regardless of the value of
            :param:`use_private_memory` in :func:`creator.__call__`
        """

        self.name = name
        self.dtype = dtype
        if not isinstance(shape, tuple):
            shape = (shape,)
        self.shape = shape
        self.scope = scope
        self.initializer = initializer
        self.fixed_indicies = None
        self.num_indicies = len(shape)
        self.order = order
        self.affine = affine
        self.is_input_or_output = is_input_or_output
        if fixed_indicies is not None:
            self.fixed_indicies = fixed_indicies[:]
        if is_temporary or initializer is not None:
            self.creator = self.__temp_var_creator
            if initializer is not None:
                assert dtype == initializer.dtype, (
                    'Incorrect dtype specified for {}, got: {} expected: {}'
                    .format(name, initializer.dtype, dtype))
                assert shape == initializer.shape, (
                    'Incorrect shape specified for {}, got: {} expected: {}'
                    .format(name, initializer.shape, shape))
        else:
            self.creator = self.__glob_arg_creator

    def __get_indicies(self, *indicies):
        if self.fixed_indicies:
            inds = [None for i in self.shape]
            for i, v in self.fixed_indicies:
                inds[i] = v
            empty = [i for i, x in enumerate(inds) if x is None]
            assert len(empty) == len(indicies), (
                'Wrong number of '
                'indicies supplied for {}: expected {} got {}'.format(
                    self.name, len(empty), len(indicies)))
            for i, ind in enumerate(empty):
                inds[ind] = indicies[i]
            return inds
        else:
            assert len(indicies) == self.num_indicies, (
                'Wrong number of indicies supplied for {}: expected {} got {}'
                .format(self.name, len(self.shape), len(indicies)))
            return indicies[:]

    def __temp_var_creator(self, **kwargs):
        # set default args
        arg_dict = {'shape': self.shape,
                    'dtype': self.dtype,
                    'order': self.order,
                    'initializer': self.initializer,
                    'scope': self.scope,
                    'read_only': self.initializer is not None}

        # and update any supplied overrides
        arg_dict.update(kwargs)
        return lp.TemporaryVariable(self.name, **arg_dict)

    def __glob_arg_creator(self, **kwargs):
        # set default args
        arg_dict = {'shape': self.shape,
                    'dtype': self.dtype,
                    'order': self.order}
        # and update any supplied overrides
        arg_dict.update(kwargs)
        return lp.GlobalArg(self.name, **arg_dict)

    def __call__(self, *indicies, **kwargs):
        # figure out whether to use private memory or not
        use_private_memory = kwargs.pop('use_private_memory', False)
        inds = self.__get_indicies(*indicies)

        # handle private memory request
        glob_ind = None
        if use_private_memory and not self.is_input_or_output:
            # find the global ind if there
            glob_ind = next((i for i, ind in enumerate(inds) if ind == global_ind),
                            None)

        if glob_ind is not None:
            # need to remove any index corresponding to the global_ind
            inds = tuple(ind for i, ind in enumerate(inds) if i != glob_ind)
            shape = tuple(s for i, s in enumerate(self.shape) if i != glob_ind)
            lp_arr = self.__temp_var_creator(shape=shape,
                                             scope=scopes.PRIVATE, **kwargs)
        else:
            lp_arr = self.creator(**kwargs)

        return (lp_arr, lp_arr.name + '[{}]'.format(', '.join(
            str(x) for x in inds)))

    def copy(self):
        return copy.deepcopy(self)


def _make_mask(map_arr, mask_size):
    """
    Create a mask array from the given map and total mask size
    """

    assert len(map_arr.shape) == 1, "Can't make mask from 2-D array"

    mask = np.full(mask_size, -1, dtype=np.int32)
    mask[map_arr] = np.arange(map_arr.size, dtype=np.int32)
    return mask


class NameStore(object):

    """
    A convenience class that simplifies loopy array creation, indexing, mapping
    and masking

    Attributes
    ----------
    loopy_opts : :class:`LoopyOptions`
        The loopy options object describing the kernels
    rate_info : dict of reaction/species rate parameters
        Keys are 'simple', 'plog', 'cheb', 'fall', 'chem', 'thd'
        Values are further dictionaries including addtional rate info, number,
        offset, maps, etc.
    order : ['C', 'F']
        The row/column-major data format to use in storage
    conp : Boolean [True]
        If true, use the constant pressure formulation
    test_size : str or int
        Optional size used in testing.  If not supplied, this is a kernel arg
    use_private_memory : Bool [False]
        If True, use _private_ OpenCL/CUDA/etc. memory for array creation.
        If False, use _global_ memory.
    """

    def __init__(self, loopy_opts, rate_info, conp=True,
                 test_size='problem_size'):
        self.loopy_opts = loopy_opts
        self.use_private_memory = loopy_opts.use_private_memory
        self.rate_info = rate_info
        self.order = loopy_opts.order
        self.test_size = test_size
        self.conp = conp
        self._add_arrays(rate_info, test_size)

    def __getattr__(self, name):
        """
        Override of getattr such that NameStore.nonexistantkey -> None
        """
        try:
            return super(NameStore, self).__getattr__(self, name)
        except AttributeError:
            return None

    def __check(self, add_map=True):
        """ Ensures that maps are only added to map kernels etc. """
        if add_map:
            assert self.loopy_opts.knl_type == 'map', ('Cannot add'
                                                       ' map to mask kernel')
        else:
            assert self.loopy_opts.knl_type == 'mask', ('Cannot add'
                                                        ' mask to map kernel')

    def __make_offset(self, arr):
        """
        Creates an offset array from the given array
        """

        assert len(arr.shape) == 1, "Can't make offset from 2-D array"
        assert arr.dtype == np.int32, "Offset arrays should be integers!"

        return np.array(np.concatenate(
            (np.cumsum(arr) - arr, np.array([np.sum(arr)]))),
            dtype=np.int32)

    def _add_arrays(self, rate_info, test_size):
        """
        Initialize the various arrays needed for the namestore
        """

        # problem size
        if isinstance(test_size, str):
            self.problem_size = problem_size

        # generic ranges
        self.num_specs = creator('num_specs', shape=(rate_info['Ns'],),
                                 dtype=np.int32, order=self.order,
                                 initializer=np.arange(rate_info['Ns'],
                                                       dtype=np.int32))
        self.num_specs_no_ns = creator('num_specs_no_ns',
                                       shape=(rate_info['Ns'] - 1,),
                                       dtype=np.int32, order=self.order,
                                       initializer=np.arange(
                                           rate_info['Ns'] - 1,
                                           dtype=np.int32))
        self.num_reacs = creator('num_reacs', shape=(rate_info['Nr'],),
                                 dtype=np.int32, order=self.order,
                                 initializer=np.arange(rate_info['Nr'],
                                                       dtype=np.int32))
        self.num_rev_reacs = creator('num_rev_reacs',
                                     shape=(rate_info['rev']['num'],),
                                     dtype=np.int32, order=self.order,
                                     initializer=np.arange(
                                         rate_info['rev']['num'],
                                         dtype=np.int32))

        self.phi_inds = creator('phi_inds',
                                shape=(rate_info['Ns'] + 1),
                                dtype=np.int32, order=self.order,
                                initializer=np.arange(rate_info['Ns'] + 1,
                                                      dtype=np.int32))
        self.phi_spec_inds = creator('phi_spec_inds',
                                     shape=(rate_info['Ns'] - 1,),
                                     dtype=np.int32, order=self.order,
                                     initializer=np.arange(2,
                                                           rate_info['Ns'] + 1,
                                                           dtype=np.int32))

        # flat / dense jacobian
        if 'jac_inds' in rate_info:
            # if we're actually creating a jacobian
            flat_row_inds = rate_info['jac_inds']['flat'][:, 0]
            flat_col_inds = rate_info['jac_inds']['flat'][:, 1]
            self.num_nonzero_jac_inds = creator('num_jac_entries',
                                                shape=flat_row_inds.shape,
                                                dtype=np.int32,
                                                order=self.order,
                                                initializer=np.arange(
                                                    flat_row_inds.size,
                                                    dtype=np.int32))
            self.flat_jac_row_inds = creator('jac_row_inds',
                                             shape=flat_row_inds.shape,
                                             dtype=np.int32,
                                             order=self.order,
                                             initializer=flat_row_inds)
            self.flat_jac_col_inds = creator('jac_col_inds',
                                             shape=flat_col_inds.shape,
                                             dtype=np.int32,
                                             order=self.order,
                                             initializer=flat_col_inds)

            # Compressed Row Storage jacobian
            crs_col_ind = rate_info['jac_inds']['crs']['col_ind']
            self.crs_jac_col_ind = creator('jac_col_inds',
                                           shape=crs_col_ind.shape,
                                           dtype=np.int32,
                                           order=self.order,
                                           initializer=crs_col_ind)
            crs_row_ptr = rate_info['jac_inds']['crs']['row_ptr']
            self.crs_jac_row_ptr = creator('jac_row_ptr',
                                           shape=crs_row_ptr.shape,
                                           dtype=np.int32,
                                           order=self.order,
                                           initializer=crs_row_ptr)

            # Compressed Column Storage jacobian
            ccs_row_ind = rate_info['jac_inds']['ccs']['row_ind']
            self.ccs_jac_row_ind = creator('jac_row_inds',
                                           shape=ccs_row_ind.shape,
                                           dtype=np.int32,
                                           order=self.order,
                                           initializer=ccs_row_ind)
            ccs_col_ptr = rate_info['jac_inds']['ccs']['col_ptr']
            self.ccs_jac_col_ptr = creator('jac_col_ptr',
                                           shape=ccs_col_ptr.shape,
                                           dtype=np.int32,
                                           order=self.order,
                                           initializer=ccs_col_ptr)

        # state arrays
        self.T_arr = creator('phi', shape=(test_size, rate_info['Ns'] + 1),
                             dtype=np.float64, order=self.order,
                             fixed_indicies=[(1, 0)],
                             is_input_or_output=True)

        # handle extra variable and P / V arrays
        self.E_arr = creator('phi', shape=(test_size, rate_info['Ns'] + 1),
                             dtype=np.float64, order=self.order,
                             fixed_indicies=[(1, 1)],
                             is_input_or_output=True)
        if self.conp:
            self.P_arr = creator('P_arr', shape=(test_size,),
                                 dtype=np.float64, order=self.order,
                                 is_input_or_output=True)
            self.V_arr = self.E_arr
        else:
            self.P_arr = self.E_arr
            self.V_arr = creator('V_arr', shape=(test_size,),
                                 dtype=np.float64, order=self.order,
                                 is_input_or_output=True)

        self.n_arr = creator('phi', shape=(test_size, rate_info['Ns'] + 1),
                             dtype=np.float64, order=self.order,
                             is_input_or_output=True)
        self.conc_arr = creator('conc', shape=(test_size, rate_info['Ns']),
                                dtype=np.float64, order=self.order)
        self.conc_ns_arr = creator('conc', shape=(test_size, rate_info['Ns']),
                                   dtype=np.float64, order=self.order,
                                   fixed_indicies=[(1, rate_info['Ns'] - 1)])
        self.n_dot = creator('dphi', shape=(test_size, rate_info['Ns'] + 1),
                             dtype=np.float64, order=self.order,
                             is_input_or_output=True)
        self.T_dot = creator('dphi', shape=(test_size, rate_info['Ns'] + 1),
                             dtype=np.float64, order=self.order,
                             fixed_indicies=[(1, 0)],
                             is_input_or_output=True)
        self.E_dot = creator('dphi', shape=(test_size, rate_info['Ns'] + 1),
                             dtype=np.float64, order=self.order,
                             fixed_indicies=[(1, 1)],
                             is_input_or_output=True)

        self.jac = creator('jac',
                           shape=(
                               test_size, rate_info['Ns'] + 1, rate_info['Ns'] + 1),
                           order=self.order,
                           dtype=np.float64,
                           is_input_or_output=True)

        self.spec_rates = creator('wdot', shape=(test_size, rate_info['Ns']),
                                  dtype=np.float64, order=self.order)

        # molecular weights
        self.mw_arr = creator('mw', shape=(rate_info['Ns'],),
                              initializer=rate_info['mws'],
                              dtype=np.float64,
                              order=self.order)

        self.mw_post_arr = creator('mw_factor', shape=(rate_info['Ns'] - 1,),
                                   initializer=rate_info['mw_post'],
                                   dtype=np.float64,
                                   order=self.order)

        # net species rates data

        # per reaction
        self.rxn_to_spec = creator('rxn_to_spec',
                                   dtype=np.int32,
                                   shape=rate_info['net'][
                                       'reac_to_spec'].shape,
                                   initializer=rate_info[
                                       'net']['reac_to_spec'],
                                   order=self.order)
        off = self.__make_offset(rate_info['net']['num_reac_to_spec'])
        self.rxn_to_spec_offsets = creator('net_reac_to_spec_offsets',
                                           dtype=np.int32,
                                           shape=off.shape,
                                           initializer=off,
                                           order=self.order)
        self.rxn_to_spec_reac_nu = creator('reac_to_spec_nu',
                                           dtype=np.int32, shape=rate_info[
                                               'net']['nu'].shape,
                                           initializer=rate_info['net']['nu'],
                                           order=self.order,
                                           affine=1)
        self.rxn_to_spec_prod_nu = creator('reac_to_spec_nu',
                                           dtype=np.int32, shape=rate_info[
                                               'net']['nu'].shape,
                                           initializer=rate_info['net']['nu'],
                                           order=self.order)

        self.rxn_has_ns = creator('rxn_has_ns',
                                  dtype=np.int32,
                                  shape=rate_info['reac_has_ns'].shape,
                                  initializer=rate_info['reac_has_ns'],
                                  order=self.order)
        self.num_rxn_has_ns = creator('num_rxn_has_ns',
                                      dtype=np.int32,
                                      shape=rate_info['reac_has_ns'].shape,
                                      initializer=np.arange(
                                          rate_info['reac_has_ns'].size,
                                          dtype=np.int32),
                                      order=self.order)

        # per species
        self.num_net_nonzero_spec = creator('num_net_nonzero_spec',
                                            dtype=np.int32,
                                            shape=rate_info['net_per_spec'][
                                                'map'].shape,
                                            initializer=np.arange(
                                                rate_info['net_per_spec'][
                                                    'map'].size,
                                                dtype=np.int32),
                                            order=self.order)
        self.net_nonzero_spec = creator('net_nonzero_spec', dtype=np.int32,
                                        shape=rate_info['net_per_spec'][
                                            'map'].shape,
                                        initializer=rate_info[
                                            'net_per_spec']['map'],
                                        order=self.order)
        self.net_nonzero_phi = creator('net_nonzero_phi', dtype=np.int32,
                                       shape=rate_info['net_per_spec'][
                                           'map'].shape,
                                       initializer=rate_info[
                                           'net_per_spec']['map'] + 2,
                                       order=self.order)

        self.spec_to_rxn = creator('spec_to_rxn', dtype=np.int32,
                                   shape=rate_info['net_per_spec'][
                                       'reacs'].shape,
                                   initializer=rate_info[
                                       'net_per_spec']['reacs'],
                                   order=self.order)
        off = self.__make_offset(rate_info['net_per_spec']['reac_count'])
        self.spec_to_rxn_offsets = creator('spec_to_rxn_offsets',
                                           dtype=np.int32,
                                           shape=off.shape,
                                           initializer=off,
                                           order=self.order)
        self.spec_to_rxn_nu = creator('spec_to_rxn_nu',
                                      dtype=np.int32, shape=rate_info[
                                          'net_per_spec']['nu'].shape,
                                      initializer=rate_info[
                                          'net_per_spec']['nu'],
                                      order=self.order)

        # rop's and fwd / rev / thd maps
        self.rop_net = creator('rop_net',
                               dtype=np.float64,
                               shape=(test_size, rate_info['Nr']),
                               order=self.order)

        self.rop_fwd = creator('rop_fwd',
                               dtype=np.float64,
                               shape=(test_size, rate_info['Nr']),
                               order=self.order)

        if rate_info['rev']['num']:
            self.rop_rev = creator('rop_rev',
                                   dtype=np.float64,
                                   shape=(
                                       test_size, rate_info['rev']['num']),
                                   order=self.order)
            self.rev_map = creator('rev_map',
                                   dtype=np.int32,
                                   shape=rate_info['rev']['map'].shape,
                                   initializer=rate_info[
                                       'rev']['map'],
                                   order=self.order)

            mask = _make_mask(rate_info['rev']['map'],
                              rate_info['Nr'])
            self.rev_mask = creator('rev_mask',
                                    dtype=np.int32,
                                    shape=mask.shape,
                                    initializer=mask,
                                    order=self.order)

        if rate_info['thd']['num']:
            self.pres_mod = creator('pres_mod',
                                    dtype=np.float64,
                                    shape=(
                                        test_size, rate_info['thd']['num']),
                                    order=self.order)
            self.thd_map = creator('thd_map',
                                   dtype=np.int32,
                                   shape=rate_info['thd']['map'].shape,
                                   initializer=rate_info[
                                       'thd']['map'],
                                   order=self.order)

            mask = _make_mask(rate_info['thd']['map'],
                              rate_info['Nr'])
            self.thd_mask = creator('thd_mask',
                                    dtype=np.int32,
                                    shape=mask.shape,
                                    initializer=mask,
                                    order=self.order)

            thd_inds = np.arange(rate_info['thd']['num'], dtype=np.int32)
            self.thd_inds = creator('thd_inds',
                                    dtype=np.int32,
                                    shape=thd_inds.shape,
                                    initializer=thd_inds,
                                    order=self.order)

        # reaction data (fwd / rev rates, KC)
        self.kf = creator('kf',
                          dtype=np.float64,
                          shape=(test_size, rate_info['Nr']),
                          order=self.order)

        # simple reaction parameters
        self.simple_A = creator('simple_A',
                                dtype=rate_info['simple']['A'].dtype,
                                shape=rate_info['simple']['A'].shape,
                                initializer=rate_info['simple']['A'],
                                order=self.order)
        self.simple_beta = creator('simple_beta',
                                   dtype=rate_info[
                                       'simple']['b'].dtype,
                                   shape=rate_info[
                                       'simple']['b'].shape,
                                   initializer=rate_info[
                                       'simple']['b'],
                                   order=self.order)
        self.simple_Ta = creator('simple_Ta',
                                 dtype=rate_info['simple']['Ta'].dtype,
                                 shape=rate_info['simple']['Ta'].shape,
                                 initializer=rate_info['simple']['Ta'],
                                 order=self.order)
        # reaction types
        self.simple_rtype = creator('simple_rtype',
                                    dtype=rate_info[
                                        'simple']['type'].dtype,
                                    shape=rate_info[
                                        'simple']['type'].shape,
                                    initializer=rate_info[
                                        'simple']['type'],
                                    order=self.order)

        # num simple
        num_simple = np.arange(rate_info['simple']['num'], dtype=np.int32)
        self.num_simple = creator('num_simple',
                                  dtype=np.int32,
                                  shape=num_simple.shape,
                                  initializer=num_simple,
                                  order=self.order)

        # simple map
        self.simple_map = creator('simple_map',
                                  dtype=np.int32,
                                  shape=rate_info['simple']['map'].shape,
                                  initializer=rate_info['simple']['map'],
                                  order=self.order)
        # simple mask
        simple_mask = _make_mask(rate_info['simple']['map'],
                                 rate_info['Nr'])
        self.simple_mask = creator('simple_mask',
                                   dtype=simple_mask.dtype,
                                   shape=simple_mask.shape,
                                   initializer=simple_mask,
                                   order=self.order)

        self.num_simple = creator('num_simple',
                                  dtype=np.int32,
                                  shape=num_simple.shape,
                                  initializer=num_simple,
                                  order=self.order)

        # rtype maps
        for rtype in np.unique(rate_info['simple']['type']):
            # find the map
            mapv = rate_info['simple']['map'][
                np.where(rate_info['simple']['type'] == rtype)[0]]
            setattr(self, 'simple_rtype_{}_map'.format(rtype),
                    creator('simple_rtype_{}_map'.format(rtype),
                            dtype=mapv.dtype,
                            shape=mapv.shape,
                            initializer=mapv,
                            order=self.order))
            # and the mask
            maskv = _make_mask(mapv, rate_info['Nr'])
            setattr(self, 'simple_rtype_{}_mask'.format(rtype),
                    creator('simple_rtype_{}_mask'.format(rtype),
                            dtype=maskv.dtype,
                            shape=maskv.shape,
                            initializer=maskv,
                            order=self.order))
            # and indicies inside of the simple parameters
            inds = np.where(
                np.in1d(rate_info['simple']['map'], mapv))[0].astype(
                dtype=np.int32)
            setattr(self, 'simple_rtype_{}_inds'.format(rtype),
                    creator('simple_rtype_{}_inds'.format(rtype),
                            dtype=inds.dtype,
                            shape=inds.shape,
                            initializer=inds,
                            order=self.order))
            # and num
            num = np.arange(inds.size, dtype=np.int32)
            setattr(self, 'simple_rtype_{}_num'.format(rtype),
                    creator('simple_rtype_{}_num'.format(rtype),
                            dtype=num.dtype,
                            shape=num.shape,
                            initializer=num,
                            order=self.order))

        if rate_info['rev']['num']:
            self.kr = creator('kr',
                              dtype=np.float64,
                              shape=(test_size, rate_info['rev']['num']),
                              order=self.order)

            self.Kc = creator('Kc',
                              dtype=np.float64,
                              shape=(test_size, rate_info['rev']['num']),
                              order=self.order)

            self.nu_sum = creator('nu_sum',
                                  dtype=rate_info['net'][
                                      'nu_sum'].dtype,
                                  shape=rate_info['net'][
                                      'nu_sum'].shape,
                                  initializer=rate_info[
                                      'net']['nu_sum'],
                                  order=self.order)

        # third body concs, maps, efficiencies, types, species
        if rate_info['thd']['num']:
            # third body concentrations
            self.thd_conc = creator('thd_conc',
                                    dtype=np.float64,
                                    shape=(
                                        test_size, rate_info['thd']['num']),
                                    order=self.order)

            # thd only indicies
            mapv = np.where(np.logical_not(np.in1d(rate_info['thd']['map'],
                                                   rate_info['fall']['map'])))[0]
            mapv = np.array(mapv, dtype=np.int32)
            self.thd_only_map = creator('thd_only_map',
                                        dtype=np.int32,
                                        shape=mapv.shape,
                                        initializer=mapv,
                                        order=self.order)
            self.num_thd_only = creator('num_thd_only',
                                        dtype=np.int32,
                                        shape=mapv.shape,
                                        initializer=np.arange(mapv.size,
                                                              dtype=np.int32),
                                        order=self.order)

            mask = _make_mask(mapv, rate_info['Nr'])
            self.thd_only_mask = creator('thd_only_mask',
                                         dtype=np.int32,
                                         shape=mask.shape,
                                         initializer=mask,
                                         order=self.order)

            num_specs = rate_info['thd']['spec_num'].astype(dtype=np.int32)
            spec_list = rate_info['thd']['spec'].astype(
                dtype=np.int32)
            thd_effs = rate_info['thd']['eff']

            # finally create arrays
            self.thd_eff = creator('thd_eff',
                                   dtype=thd_effs.dtype,
                                   shape=thd_effs.shape,
                                   initializer=thd_effs,
                                   order=self.order)
            num_thd = np.arange(rate_info['thd']['num'], dtype=np.int32)
            self.num_thd = creator('num_thd',
                                   dtype=num_thd.dtype,
                                   shape=num_thd.shape,
                                   initializer=num_thd,
                                   order=self.order)
            thd_only_ns_inds = np.where(
                np.in1d(
                    self.thd_only_map.initializer,
                    rate_info['thd']['has_ns']))[0].astype(np.int32)
            thd_only_ns_map = self.thd_only_map.initializer[
                thd_only_ns_inds]
            self.thd_only_ns_map = creator('thd_only_ns_map',
                                           dtype=thd_only_ns_map.dtype,
                                           shape=thd_only_ns_map.shape,
                                           initializer=thd_only_ns_map,
                                           order=self.order)

            self.thd_only_ns_inds = creator('thd_only_ns_inds',
                                            dtype=thd_only_ns_inds.dtype,
                                            shape=thd_only_ns_inds.shape,
                                            initializer=thd_only_ns_inds,
                                            order=self.order)
            self.thd_type = creator('thd_type',
                                    dtype=rate_info['thd']['type'].dtype,
                                    shape=rate_info['thd']['type'].shape,
                                    initializer=rate_info['thd']['type'],
                                    order=self.order)
            self.thd_spec = creator('thd_spec',
                                    dtype=spec_list.dtype,
                                    shape=spec_list.shape,
                                    initializer=spec_list,
                                    order=self.order)
            thd_offset = self.__make_offset(num_specs)
            self.thd_offset = creator('thd_offset',
                                      dtype=thd_offset.dtype,
                                      shape=thd_offset.shape,
                                      initializer=thd_offset,
                                      order=self.order)

        # falloff rxn rates, blending vals, reduced pressures, maps
        if rate_info['fall']['num']:
            # falloff reaction parameters
            self.kf_fall = creator('kf_fall',
                                   dtype=np.float64,
                                   shape=(test_size, rate_info['fall']['num']),
                                   order=self.order)
            self.fall_A = creator('fall_A',
                                  dtype=rate_info['fall']['A'].dtype,
                                  shape=rate_info['fall']['A'].shape,
                                  initializer=rate_info['fall']['A'],
                                  order=self.order)
            self.fall_beta = creator('fall_beta',
                                     dtype=rate_info[
                                         'fall']['b'].dtype,
                                     shape=rate_info[
                                         'fall']['b'].shape,
                                     initializer=rate_info[
                                         'fall']['b'],
                                     order=self.order)
            self.fall_Ta = creator('fall_Ta',
                                   dtype=rate_info['fall']['Ta'].dtype,
                                   shape=rate_info['fall']['Ta'].shape,
                                   initializer=rate_info['fall']['Ta'],
                                   order=self.order)
            # reaction types
            self.fall_rtype = creator('fall_rtype',
                                      dtype=rate_info[
                                          'fall']['type'].dtype,
                                      shape=rate_info[
                                          'fall']['type'].shape,
                                      initializer=rate_info[
                                          'fall']['type'],
                                      order=self.order)

            # fall mask
            fall_mask = _make_mask(rate_info['fall']['map'],
                                   rate_info['Nr'])
            self.fall_mask = creator('fall_mask',
                                     dtype=fall_mask.dtype,
                                     shape=fall_mask.shape,
                                     initializer=fall_mask,
                                     order=self.order)

            # rtype maps
            for rtype in np.unique(rate_info['fall']['type']):
                # find the map in global reaction index
                mapv = rate_info['fall']['map'][
                    np.where(rate_info['fall']['type'] == rtype)[0]]
                setattr(self, 'fall_rtype_{}_map'.format(rtype),
                        creator('fall_rtype_{}_map'.format(rtype),
                                dtype=mapv.dtype,
                                shape=mapv.shape,
                                initializer=mapv,
                                order=self.order))
                # create corresponding mask
                maskv = _make_mask(mapv, rate_info['Nr'])
                setattr(self, 'fall_rtype_{}_mask'.format(rtype),
                        creator('fall_rtype_{}_mask'.format(rtype),
                                dtype=maskv.dtype,
                                shape=maskv.shape,
                                initializer=maskv,
                                order=self.order))
                # and indicies inside of the falloff parameters
                inds = np.where(rate_info['fall']['map'] == mapv)[0].astype(
                    dtype=np.int32)
                setattr(self, 'fall_rtype_{}_inds'.format(rtype),
                        creator('fall_rtype_{}_inds'.format(rtype),
                                dtype=inds.dtype,
                                shape=inds.shape,
                                initializer=inds,
                                order=self.order))
                # and num
                num = np.arange(inds.size, dtype=np.int32)
                setattr(self, 'fall_rtype_{}_num'.format(rtype),
                        creator('fall_rtype_{}_num'.format(rtype),
                                dtype=num.dtype,
                                shape=num.shape,
                                initializer=num,
                                order=self.order))

            # maps
            self.fall_map = creator('fall_map',
                                    dtype=np.int32,
                                    initializer=rate_info['fall']['map'],
                                    shape=rate_info['fall']['map'].shape,
                                    order=self.order)

            num_fall = np.arange(rate_info['fall']['num'], dtype=np.int32)
            self.num_fall = creator('num_fall',
                                    dtype=np.int32,
                                    initializer=num_fall,
                                    shape=num_fall.shape,
                                    order=self.order)

            # blending
            self.Fi = creator('Fi',
                              dtype=np.float64,
                              shape=(test_size, rate_info['fall']['num']),
                              order=self.order)

            # reduced pressure
            self.Pr = creator('Pr',
                              dtype=np.float64,
                              shape=(test_size, rate_info['fall']['num']),
                              order=self.order)

            # types
            self.fall_type = creator('fall_type',
                                     dtype=rate_info[
                                         'fall']['ftype'].dtype,
                                     shape=rate_info[
                                         'fall']['ftype'].shape,
                                     initializer=rate_info[
                                         'fall']['ftype'],
                                     order=self.order)

            # maps and masks
            fall_to_thd_map = np.array(
                np.where(
                    np.in1d(
                        rate_info['thd']['map'], rate_info['fall']['map'])
                )[0], dtype=np.int32)
            self.fall_to_thd_map = creator('fall_to_thd_map',
                                           dtype=np.int32,
                                           initializer=fall_to_thd_map,
                                           shape=fall_to_thd_map.shape,
                                           order=self.order)

            fall_to_thd_mask = _make_mask(fall_to_thd_map,
                                          rate_info['Nr'])
            self.fall_to_thd_mask = creator('fall_to_thd_mask',
                                            dtype=np.int32,
                                            initializer=fall_to_thd_mask,
                                            shape=fall_to_thd_mask.shape,
                                            order=self.order)

            if rate_info['fall']['troe']['num']:
                # Fcent, Atroe, Btroe
                self.Fcent = creator('Fcent',
                                     shape=(test_size,
                                            rate_info['fall']['troe']['num']),
                                     dtype=np.float64,
                                     order=self.order)

                self.Atroe = creator('Atroe',
                                     shape=(test_size,
                                            rate_info['fall']['troe']['num']),
                                     dtype=np.float64,
                                     order=self.order)

                self.Btroe = creator('Btroe',
                                     shape=(test_size,
                                            rate_info['fall']['troe']['num']),
                                     dtype=np.float64,
                                     order=self.order)

                # troe parameters
                self.troe_a = creator('troe_a',
                                      shape=rate_info['fall'][
                                          'troe']['a'].shape,
                                      dtype=rate_info['fall'][
                                          'troe']['a'].dtype,
                                      initializer=rate_info[
                                          'fall']['troe']['a'],
                                      order=self.order)
                self.troe_T1 = creator('troe_T1',
                                       shape=rate_info['fall'][
                                           'troe']['T1'].shape,
                                       dtype=rate_info['fall'][
                                           'troe']['T1'].dtype,
                                       initializer=rate_info[
                                           'fall']['troe']['T1'],
                                       order=self.order)
                self.troe_T3 = creator('troe_T3',
                                       shape=rate_info['fall'][
                                           'troe']['T3'].shape,
                                       dtype=rate_info['fall'][
                                           'troe']['T3'].dtype,
                                       initializer=rate_info[
                                           'fall']['troe']['T3'],
                                       order=self.order)
                self.troe_T2 = creator('troe_T2',
                                       shape=rate_info['fall'][
                                           'troe']['T2'].shape,
                                       dtype=rate_info['fall'][
                                           'troe']['T2'].dtype,
                                       initializer=rate_info[
                                           'fall']['troe']['T2'],
                                       order=self.order)

                # map and mask
                num_troe = np.arange(rate_info['fall']['troe']['num'],
                                     dtype=np.int32)
                self.num_troe = creator('num_troe',
                                        shape=num_troe.shape,
                                        dtype=num_troe.dtype,
                                        initializer=num_troe,
                                        order=self.order)
                self.troe_map = creator('troe_map',
                                        shape=rate_info['fall'][
                                            'troe']['map'].shape,
                                        dtype=rate_info['fall'][
                                            'troe']['map'].dtype,
                                        initializer=rate_info[
                                            'fall']['troe']['map'],
                                        order=self.order)
                troe_mask = _make_mask(rate_info['fall']['troe']['map'],
                                       rate_info['Nr'])
                self.troe_mask = creator('troe_mask',
                                         shape=troe_mask.shape,
                                         dtype=troe_mask.dtype,
                                         initializer=troe_mask,
                                         order=self.order)

                troe_inds = self.fall_to_thd_map.initializer[
                    self.troe_map.initializer]
                troe_ns_inds = np.where(
                    np.in1d(troe_inds, rate_info['thd']['has_ns']))[0].astype(
                        np.int32)
                troe_has_ns = self.troe_map.initializer[
                    troe_ns_inds]
                self.troe_has_ns = creator('troe_has_ns',
                                           shape=troe_has_ns.shape,
                                           dtype=troe_has_ns.dtype,
                                           initializer=troe_has_ns,
                                           order=self.order)
                self.troe_ns_inds = creator('troe_ns_inds',
                                            shape=troe_ns_inds.shape,
                                            dtype=troe_ns_inds.dtype,
                                            initializer=troe_ns_inds,
                                            order=self.order)

            if rate_info['fall']['sri']['num']:
                # X_sri
                self.X_sri = creator('X',
                                     shape=(test_size,
                                            rate_info['fall']['sri']['num']),
                                     dtype=np.float64,
                                     order=self.order)

                # sri parameters
                self.sri_a = creator('sri_a',
                                     shape=rate_info['fall'][
                                         'sri']['a'].shape,
                                     dtype=rate_info['fall'][
                                         'sri']['a'].dtype,
                                     initializer=rate_info[
                                         'fall']['sri']['a'],
                                     order=self.order)
                self.sri_b = creator('sri_b',
                                     shape=rate_info['fall'][
                                         'sri']['b'].shape,
                                     dtype=rate_info['fall'][
                                         'sri']['b'].dtype,
                                     initializer=rate_info[
                                         'fall']['sri']['b'],
                                     order=self.order)
                self.sri_c = creator('sri_c',
                                     shape=rate_info['fall'][
                                         'sri']['c'].shape,
                                     dtype=rate_info['fall'][
                                         'sri']['c'].dtype,
                                     initializer=rate_info[
                                         'fall']['sri']['c'],
                                     order=self.order)
                self.sri_d = creator('sri_d',
                                     shape=rate_info['fall'][
                                         'sri']['d'].shape,
                                     dtype=rate_info['fall'][
                                         'sri']['d'].dtype,
                                     initializer=rate_info[
                                         'fall']['sri']['d'],
                                     order=self.order)
                self.sri_e = creator('sri_e',
                                     shape=rate_info['fall'][
                                         'sri']['e'].shape,
                                     dtype=rate_info['fall'][
                                         'sri']['e'].dtype,
                                     initializer=rate_info[
                                         'fall']['sri']['e'],
                                     order=self.order)

                # map and mask
                num_sri = np.arange(rate_info['fall']['sri']['num'],
                                    dtype=np.int32)
                self.num_sri = creator('num_sri',
                                       shape=num_sri.shape,
                                       dtype=num_sri.dtype,
                                       initializer=num_sri,
                                       order=self.order)
                self.sri_map = creator('sri_map',
                                       shape=rate_info['fall'][
                                           'sri']['map'].shape,
                                       dtype=rate_info['fall'][
                                           'sri']['map'].dtype,
                                       initializer=rate_info[
                                           'fall']['sri']['map'],
                                       order=self.order)
                sri_mask = _make_mask(rate_info['fall']['sri']['map'],
                                      rate_info['Nr'])
                self.sri_mask = creator('sri_mask',
                                        shape=sri_mask.shape,
                                        dtype=sri_mask.dtype,
                                        initializer=sri_mask,
                                        order=self.order)

                sri_inds = self.fall_to_thd_map.initializer[
                    self.sri_map.initializer]
                sri_ns_inds = np.where(
                    np.in1d(sri_inds, rate_info['thd']['has_ns']))[0].astype(
                        np.int32)
                sri_has_ns = self.sri_map.initializer[
                    sri_ns_inds]
                self.sri_has_ns = creator('sri_has_ns_map',
                                          shape=sri_has_ns.shape,
                                          dtype=sri_has_ns.dtype,
                                          initializer=sri_has_ns,
                                          order=self.order)
                self.sri_ns_inds = creator('sri_ns_inds',
                                           shape=sri_ns_inds.shape,
                                           dtype=sri_ns_inds.dtype,
                                           initializer=sri_ns_inds,
                                           order=self.order)

            if rate_info['fall']['lind']['num']:
                # lind map / mask
                self.lind_map = creator('lind_map',
                                        shape=rate_info['fall'][
                                            'lind']['map'].shape,
                                        dtype=rate_info['fall'][
                                            'lind']['map'].dtype,
                                        initializer=rate_info[
                                            'fall']['lind']['map'],
                                        order=self.order)
                lind_mask = _make_mask(rate_info['fall']['lind']['map'],
                                       rate_info['Nr'])
                self.lind_mask = creator('lind_mask',
                                         shape=lind_mask.shape,
                                         dtype=lind_mask.dtype,
                                         initializer=lind_mask,
                                         order=self.order)
                num_lind = np.arange(rate_info['fall']['lind']['num'],
                                     dtype=np.int32)
                self.num_lind = creator('num_lind',
                                        shape=num_lind.shape,
                                        dtype=num_lind.dtype,
                                        initializer=num_lind,
                                        order=self.order)
                lind_inds = self.fall_to_thd_map.initializer[
                    self.lind_map.initializer]
                lind_ns_inds = np.where(
                    np.in1d(lind_inds, rate_info['thd']['has_ns']))[0].astype(
                        np.int32)
                lind_has_ns = self.lind_map.initializer[
                    lind_ns_inds]
                self.lind_has_ns = creator('lind_has_ns',
                                           shape=lind_has_ns.shape,
                                           dtype=lind_has_ns.dtype,
                                           initializer=lind_has_ns,
                                           order=self.order)
                self.lind_ns_inds = creator('lind_ns_inds',
                                            shape=lind_ns_inds.shape,
                                            dtype=lind_ns_inds.dtype,
                                            initializer=lind_ns_inds,
                                            order=self.order)

        # chebyshev
        if rate_info['cheb']['num']:
            self.cheb_numP = creator('cheb_numP',
                                     dtype=rate_info[
                                         'cheb']['num_P'].dtype,
                                     initializer=rate_info[
                                         'cheb']['num_P'],
                                     shape=rate_info[
                                         'cheb']['num_P'].shape,
                                     order=self.order)

            self.cheb_numT = creator('cheb_numT',
                                     dtype=rate_info[
                                         'cheb']['num_T'].dtype,
                                     initializer=rate_info[
                                         'cheb']['num_T'],
                                     shape=rate_info[
                                         'cheb']['num_T'].shape,
                                     order=self.order)

            # chebyshev parameters
            self.cheb_params = creator('cheb_params',
                                       dtype=rate_info['cheb'][
                                           'post_process']['params'].dtype,
                                       initializer=rate_info['cheb'][
                                           'post_process']['params'],
                                       shape=rate_info['cheb'][
                                           'post_process']['params'].shape,
                                       order=self.order)

            # limits for cheby polys
            self.cheb_Plim = creator('cheb_Plim',
                                     dtype=rate_info['cheb'][
                                         'post_process']['Plim'].dtype,
                                     initializer=rate_info['cheb'][
                                         'post_process']['Plim'],
                                     shape=rate_info['cheb'][
                                         'post_process']['Plim'].shape,
                                     order=self.order)
            self.cheb_Tlim = creator('cheb_Tlim',
                                     dtype=rate_info['cheb'][
                                         'post_process']['Tlim'].dtype,
                                     initializer=rate_info['cheb'][
                                         'post_process']['Tlim'],
                                     shape=rate_info['cheb'][
                                         'post_process']['Tlim'].shape,
                                     order=self.order)

            # workspace variables
            polymax = int(np.max(np.maximum(rate_info['cheb']['num_P'],
                                            rate_info['cheb']['num_T'])))
            self.cheb_pres_poly = creator('cheb_pres_poly',
                                          dtype=np.float64,
                                          shape=(polymax,),
                                          order=self.order,
                                          is_temporary=True,
                                          scope=scopes.PRIVATE)
            self.cheb_temp_poly = creator('cheb_temp_poly',
                                          dtype=np.float64,
                                          shape=(polymax,),
                                          order=self.order,
                                          is_temporary=True,
                                          scope=scopes.PRIVATE)

            # mask and map
            cheb_map = rate_info['cheb']['map'].astype(dtype=np.int32)
            self.cheb_map = creator('cheb_map',
                                    dtype=cheb_map.dtype,
                                    initializer=cheb_map,
                                    shape=cheb_map.shape,
                                    order=self.order)
            cheb_mask = _make_mask(cheb_map, rate_info['Nr'])
            self.cheb_mask = creator('cheb_mask',
                                     dtype=cheb_mask.dtype,
                                     initializer=cheb_mask,
                                     shape=cheb_mask.shape,
                                     order=self.order)
            num_cheb = np.arange(rate_info['cheb']['num'], dtype=np.int32)
            self.num_cheb = creator('num_cheb',
                                    dtype=num_cheb.dtype,
                                    initializer=num_cheb,
                                    shape=num_cheb.shape,
                                    order=self.order)

        # plog parameters, offsets, map / mask
        if rate_info['plog']['num']:
            self.plog_params = creator('plog_params',
                                       dtype=rate_info['plog'][
                                           'post_process']['params'].dtype,
                                       initializer=rate_info['plog'][
                                           'post_process']['params'],
                                       shape=rate_info['plog'][
                                           'post_process']['params'].shape,
                                       order=self.order)

            self.plog_num_param = creator('plog_num_param',
                                          dtype=rate_info['plog'][
                                              'num_P'].dtype,
                                          initializer=rate_info['plog'][
                                              'num_P'],
                                          shape=rate_info['plog'][
                                              'num_P'].shape,
                                          order=self.order)

            # mask and map
            plog_map = rate_info['plog']['map'].astype(dtype=np.int32)
            self.plog_map = creator('plog_map',
                                    dtype=plog_map.dtype,
                                    initializer=plog_map,
                                    shape=plog_map.shape,
                                    order=self.order)
            plog_mask = _make_mask(plog_map, rate_info['Nr'])
            self.plog_mask = creator('plog_mask',
                                     dtype=plog_mask.dtype,
                                     initializer=plog_mask,
                                     shape=plog_mask.shape,
                                     order=self.order)
            num_plog = np.arange(rate_info['plog']['num'], dtype=np.int32)
            self.num_plog = creator('num_plog',
                                    dtype=num_plog.dtype,
                                    initializer=num_plog,
                                    shape=num_plog.shape,
                                    order=self.order)

        # thermodynamic properties
        self.a_lo = creator('a_lo',
                            dtype=rate_info['thermo']['a_lo'].dtype,
                            initializer=rate_info['thermo']['a_lo'],
                            shape=rate_info['thermo']['a_lo'].shape,
                            order=self.order)
        self.a_hi = creator('a_hi',
                            dtype=rate_info['thermo']['a_hi'].dtype,
                            initializer=rate_info['thermo']['a_hi'],
                            shape=rate_info['thermo']['a_hi'].shape,
                            order=self.order)
        self.T_mid = creator('T_mid',
                             dtype=rate_info['thermo']['T_mid'].dtype,
                             initializer=rate_info['thermo']['T_mid'],
                             shape=rate_info['thermo']['T_mid'].shape,
                             order=self.order)
        for name in ['cp', 'cv', 'u', 'h', 'b', 'dcp', 'dcv', 'db']:
            setattr(self, name, creator(name,
                                        dtype=np.float64,
                                        shape=(test_size, rate_info['Ns']),
                                        order=self.order))
        # thermo arrays
        self.spec_energy = self.h if self.conp else self.u
        self.spec_energy_ns = self.spec_energy.copy()
        self.spec_energy_ns.fixed_indicies = [(1, rate_info['Ns'] - 1)]
        self.spec_heat = self.cp if self.conp else self.cv
        self.spec_heat_ns = self.spec_heat.copy()
        self.spec_heat_ns.fixed_indicies = [(1, rate_info['Ns'] - 1)]
        self.spec_heat_total = creator(
            self.spec_heat.name + '_tot', shape=(test_size,),
            dtype=np.float64, order=self.order)
        self.dspec_heat = getattr(self, 'd' + self.spec_heat.name).copy()