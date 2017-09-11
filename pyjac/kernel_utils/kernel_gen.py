"""
kernel_gen.py - generators used for kernel creation
"""

import shutil
import textwrap
import os
import re
from string import Template
import logging

import six
import loopy as lp
import pyopencl as cl
import numpy as np
import cgen

from . import file_writers as filew
from .memory_manager import memory_manager
from .. import site_conf as site
from .. import utils
from ..loopy_utils import loopy_utils as lp_utils
from ..core.array_creator import problem_size as p_size
from ..core import instruction_creator as ic

script_dir = os.path.abspath(os.path.dirname(__file__))


class vecwith_fixer(object):

    """
    Simple utility class to force a constant vector width
    even when the loop being vectorized is shorted than the desired width

    clean : :class:`loopy.LoopyKernel`
        The 'clean' version of the kernel, that will be used for
        determination of the gridsize / vecwidth
    vecwidth : int
        The desired vector width
    """

    def __init__(self, clean, vecwidth):
        self.clean = clean
        self.vecwidth = vecwidth

    def __call__(self, insn_ids, ignore_auto=False):
        # fix for variable too small for vectorization
        grid_size, lsize = self.clean.get_grid_sizes_for_insn_ids(
            insn_ids, ignore_auto=ignore_auto)
        lsize = lsize if self.vecwidth is None else \
            self.vecwidth
        return grid_size, (lsize,)


def make_kernel_generator(loopy_opts, *args, **kw_args):
    """
    Factory generator method to return the appropriate
    :class:`kernel_generator` type based on the target language in the
    :param:`loopy_opts`

    Parameters
    ----------
    loopy_opts : :class:`LoopyOptions`
        The specified user options
    *args : tuple
        The other positional args to pass to the :class:`kernel_generator`
    **kw_args : dict
        The keyword args to pass to the :class:`kernel_generator`
    """
    if loopy_opts.lang == 'c':
        if not loopy_opts.auto_diff:
            return c_kernel_generator(loopy_opts, *args, **kw_args)
        if loopy_opts.auto_diff:
            return autodiff_kernel_generator(loopy_opts, *args, **kw_args)
    if loopy_opts.lang == 'opencl':
        return opencl_kernel_generator(loopy_opts, *args, **kw_args)
    if loopy_opts.lang == 'ispc':
        return ispc_kernel_generator(loopy_opts, *args, **kw_args)
    raise NotImplementedError()


class kernel_generator(object):

    """
    The base class for the kernel generators
    """

    def __init__(self, loopy_opts, name, kernels,
                 external_kernels=[],
                 input_arrays=[],
                 output_arrays=[],
                 init_arrays={},
                 test_size=None,
                 auto_diff=False,
                 depends_on=[],
                 array_props={},
                 barriers=[],
                 extra_kernel_data=[]):
        """
        Parameters
        ----------
        loopy_opts : :class:`LoopyOptions`
            The specified user options
        name : str
            The kernel name to use
        kernels : list of :class:`loopy.LoopKernel`
            The kernels / calls to wrap
        external_kernels : list of :class:`loopy.LoopKernel`
            External kernels that must be called, but not implemented in this
            file
        input_arrays : list of str
            The names of the input arrays of this kernel
        output_arrays : list of str
            The names of the output arrays of this kernel
        init_arrays : dict
            A mapping of name -> initializer value for arrays in
            this kernel that require constant value initalization
        test_size : int
            If specified, the # of conditions to test
        auto_diff : bool
            If true, this will be used for automatic differentiation
        depends_on : list of :class:`kernel_generator`
            If supplied, this kernel depends on the supplied depencies
        array_props : dict
            Mapping of various switches to array names:
                doesnt_need_init
                    * Arrays in this list do not need initialization
                      [defined for host arrays only]
        barriers : list of tuples
            List of global memory barriers needed, (knl1, knl2, barrier_type)
        extra_kernel_data : list of :class:`loopy.ArrayBase`
            Extra kernel arguements to add to this kernel
        """

        self.compiler = None
        self.loopy_opts = loopy_opts
        self.array_split = ic.array_splitter(loopy_opts)
        self.lang = loopy_opts.lang
        self.mem = memory_manager(self.lang)
        self.name = name
        self.kernels = kernels
        self.external_kernels = external_kernels
        self.test_size = test_size
        self.auto_diff = auto_diff

        # update the memory manager
        self.mem.add_arrays(in_arrays=input_arrays,
                            out_arrays=output_arrays, has_init=init_arrays)

        self.type_map = {}
        from loopy.types import to_loopy_type
        self.type_map[to_loopy_type(np.float64)] = 'double'
        self.type_map[to_loopy_type(np.int32)] = 'int'

        self.filename = ''
        self.bin_name = ''
        self.header_name = ''
        self.file_prefix = ''

        self.depends_on = depends_on[:]
        self.array_props = array_props.copy()
        self.all_arrays = []
        self.barriers = barriers[:]

        # the base skeleton for sub kernel creation
        self.skeleton = """
        for j
            ${pre}
            for ${var_name}
                ${main}
            end
            ${post}
        end
        """

        # list of inames added to sub kernels
        self.inames = ['j']

        # list of iname domains added to subkernels
        self.iname_domains = ['0<=j<{}']

        # extra kernel parameters to be added to subkernels
        self.extra_kernel_data = extra_kernel_data[:]

    def apply_barriers(self, barriers, instructions):
        """
        A method stud that can be overriden to apply synchonization barriers
        to vectorized code

        Parameters
        ----------
        barriers: list of (i0, i1, type)
            i0: int
                the index of the instruction to insert the barrier after
            i1: int
                the index of the instruction to insert the barrier before,
                i0 + 1 should be equal to i1
            type: ['global', 'local']
                 The type of memory to synchronize, typically global

        instructions: list of str
            The instructions for this kernel

        Returns
        -------

        instructions : list of str
            The instructions passed in
        """
        return instructions

    def get_assumptions(self, test_size):
        """
        Returns a list of assumptions on the loop domains
        of generated subkernels

        Parameters
        ----------
        test_size : int or str
            In testing, this should be the integer size of the test data
            For production, this should the 'test_size' (or the corresponding)
            for the variable test size passed to the kernel

        Returns
        -------

        assumptions : list of str
            List of assumptions to apply to the generated sub kernel
        """

        assumpt_list = ['{0} > 0'.format(test_size)]
        # get vector width
        vec_width = self.loopy_opts.depth if self.loopy_opts.depth \
            else self.loopy_opts.width
        if vec_width is not None:
            assumpt_list.append('{0} mod {1} = 0'.format(
                test_size, vec_width))
        return assumpt_list

    def get_inames(self, test_size):
        """
        Returns the inames and iname_ranges for subkernels created using
        this generator

        Parameters
        ----------
        test_size : int or str
            In testing, this should be the integer size of the test data
            For production, this should the 'test_size' (or the corresponding)
            for the variable test size passed to the kernel

        Returns
        -------
        inames : list of str
            The string inames to add to created subkernels by default
        iname_domains : list of str
            The iname domains to add to created subkernels by default
        """

        return self.inames, [self.iname_domains[0].format(test_size)]

    def add_depencencies(self, k_gens):
        """
        Adds the supplied :class:`kernel_generator`s to this
        one's dependency list.  Functionally this means that this kernel
        generator will know how to compile and execute functions
        from the dependencies

        Parameters
        ----------
        k_gens : list of :class:`kernel_generator`
            The dependencies to add to this kernel
        """

        self.depends_on.extend(k_gens)

    def _make_kernels(self):
        """
        Turns the supplied kernel infos into loopy kernels,
        and vectorizes them!

        Parameters
        ----------
        None

        Returns
        -------
        None
        """

        # TODO: need to update loopy to allow pointer args
        # to functions, in the meantime use a Template

        # now create the kernels!
        target = lp_utils.get_target(self.lang, self.loopy_opts.device,
                                     self.compiler)
        for i, info in enumerate(self.kernels):
            if info in self.external_kernels:
                continue
            # create kernel from k_gen.knl_info
            self.kernels[i] = self.make_kernel(info, target, self.test_size)
            # apply vectorization
            self.kernels[i] = self.apply_specialization(
                self.loopy_opts,
                info.var_name,
                self.kernels[i],
                vecspec=info.vectorization_specializer,
                can_vectorize=info.can_vectorize)

            # update the kernel args
            self.kernels[i] = self.array_split.split_loopy_arrays(self.kernels[i])

            # and add a mangler
            # func_manglers.append(create_function_mangler(kernels[i]))
            # set the editor
            self.kernels[i] = lp_utils.set_editor(self.kernels[i])

        # and finally register functions
        # for func in func_manglers:
        #    knl = lp.register_function_manglers(knl, [func])

        # need to call make_kernels on dependencies
        for x in self.depends_on:
            x._make_kernels()

    def __copy_deps(self, scan_path, out_path, change_extension=True):
        """
        Convenience function to copy the dependencies of this
        :class:`kernel_generator` to our own output path

        Parameters
        ----------

        scan_path : str
            The path the dependencies were written to
        out_path : str
            The path this generator is writing to
        change_ext : bool
            If True, any dependencies that do not end with the proper file
            extension, see :any:`utils.file_ext`

        """
        deps = [x for x in os.listdir(scan_path) if os.path.isfile(
            os.path.join(scan_path, x)) and not x.endswith('.in')]
        for dep in deps:
            dep_dest = dep
            dep_is_header = dep.endswith('.h')
            ext = (utils.file_ext[self.lang] if not dep_is_header
                   else utils.header_ext[self.lang])
            if change_extension and not dep.endswith(ext):
                dep_dest = dep[:dep.rfind('.')] + ext
            shutil.copyfile(os.path.join(scan_path, dep),
                            os.path.join(out_path, dep_dest))

    def generate(self, path, data_order=None, data_filename='data.bin'):
        """
        Generates wrapping kernel, compiling program (if necessary) and
        calling / executing program for this kernel

        Parameters
        ----------
        path : str
            The output path
        data_order : {'C', 'F'}
            If specified, the ordering of the binary input data
            which may differ from the loopy order
        data_filename : Optional[str]
            If specified, the path to the data file for reading / execution
            via the command line

        Returns
        -------
        None
        """
        utils.create_dir(path)
        self._make_kernels()
        self._generate_wrapping_kernel(path)
        self._generate_compiling_program(path)
        self._generate_calling_program(path, data_filename)
        self._generate_calling_header(path)
        self._generate_common(path)

        # finally, copy any dependencies to the path
        lang_dir = os.path.join(script_dir, self.lang)
        self.__copy_deps(lang_dir, path, change_extension=False)

    def _generate_common(self, path):
        """
        Creates the common files (used by all target languages) for this
        kernel generator

        Parameters
        ----------
        path : str
            The output path for the common files

        Returns
        -------
        None
        """

        common_dir = os.path.join(script_dir, 'common')
        # get the initial condition reader
        with open(os.path.join(common_dir,
                               'read_initial_conditions.c.in'), 'r') as file:
            file_src = Template(file.read())

        with filew.get_file(os.path.join(path, 'read_initial_conditions'
                                         + utils.file_ext[self.lang]),
                            self.lang,
                            use_filter=False) as file:
            file.add_lines(file_src.safe_substitute(
                mechanism='mechanism' + utils.header_ext[self.lang]))

        # and any other deps
        self.__copy_deps(common_dir, path)

    def _get_pass(self, argv, include_type=True, is_host=True, postfix=''):
        """
        Simple helper method to get the string for passing an arguement
        to a method (or for the method definition)

        Parameters
        ----------
        argv : :class:`loopy.KernelArgument`
            The arguement to pass
        include_type : bool
            If True, include the C-type in the pass string [Default:True]
        postfix : str
            Optional postfix to append to the variable name [Default:'']
        """
        prefix = 'h_' if is_host else 'd_'
        return '{type}{prefix}{name}'.format(
            type=self.type_map[argv.dtype] + '* ' if include_type else '',
            prefix=prefix,
            name=argv.name + postfix)

    def _generate_calling_header(self, path):
        """
        Creates the header file for this kernel

        Parameters
        ----------
        path : str
            The output path for the header file

        Returns
        -------
        None
        """
        assert self.filename or self.bin_name, ('Cannot generate calling '
                                                'header before wrapping kernel'
                                                ' is generated...')
        with open(os.path.join(script_dir, self.lang,
                               'kernel.h.in'), 'r') as file:
            file_src = Template(file.read())

        self.header_name = os.path.join(path,
                                        self.file_prefix + self.name +
                                        '_main' + utils.header_ext[
                                            self.lang])
        with filew.get_file(os.path.join(path, self.header_name), self.lang,
                            use_filter=False) as file:
            file.add_lines(file_src.safe_substitute(
                input_args=', '.join([self._get_pass(next(
                    x for x in self.mem.arrays if x.name == a))
                    for a in self.mem.host_arrays]),
                knl_name=self.name))

    def _special_kernel_subs(self, file_src):
        """
        Substitutes kernel template parameters that are specific to a
        target languages, to be specialized by subclasses of the
        :class:`kernel_generator`

        Parameters
        ----------
        file_src : Template
            The kernel source template to substitute into

        Returns:
        new_file_src : Template
            An updated kernel source template to substitute general template
            parameters into
        """
        return file_src

    def _special_wrapper_subs(self, file_src):
        """
        Substitutes wrapper kernel template parameters that are specific to a
        target languages, to be specialized by subclasses of the
        :class:`kernel_generator`

        Parameters
        ----------
        file_src : Template
            The kernel source template to substitute into

        Returns:
        new_file_src : Template
            An updated kernel source template to substitute general template
            parameters into
        """
        return file_src

    def _set_sort(self, arr):
        return sorted(set(arr), key=lambda x: arr.index(x))

    def _generate_calling_program(self, path, data_filename):
        """
        Needed for all languages, this generates a simple C file that
        reads in data, sets up the kernel call, executes, etc.

        Parameters
        ----------
        path : str
            The output path to write files to
        data_filename : str
            The path to the data file for command line input

        Returns
        -------
        None
        """

        assert self.filename or self.bin_name, (
            'Cannot generate calling program before wrapping kernel '
            'is generated...')

        # find definitions
        mem_declares = self.mem.get_defns()

        # and input args

        # these are the args in the kernel defn
        knl_args = ', '.join([self._get_pass(
            next(x for x in self.mem.arrays if x.name == a))
                              for a in self.mem.host_arrays])
        # these are the args passed to the kernel (exclude type)
        input_args = ', '.join([self._get_pass(
            next(x for x in self.mem.arrays if x.name == a),
            include_type=False) for a in self.mem.host_arrays])
        # these are passed from the main method (exclude type, add _local
        # postfix)
        local_input_args = ', '.join([self._get_pass(
            next(x for x in self.mem.arrays if x.name == a),
            include_type=False,
            postfix='_local') for a in self.mem.host_arrays])
        # create doc strings
        knl_args_doc = []
        knl_args_doc_template = Template(
            """
${name} : ${type}
    ${desc}
""")
        for x in self.mem.in_arrays:
            if x == 'phi':
                knl_args_doc.append(knl_args_doc_template.safe_substitute(
                    name=x, type='double*', desc='The state vector'))
            elif x == 'P_arr':
                knl_args_doc.append(knl_args_doc_template.safe_substitute(
                    name=x, type='double*', desc='The array of pressures'))
            elif x == 'V_arr':
                knl_args_doc.append(knl_args_doc_template.safe_substitute(
                    name=x, type='double*', desc='The array of volumes'))
            elif x == 'dphi':
                knl_args_doc.append(knl_args_doc_template.safe_substitute(
                    name=x, type='double*', desc=('The time rate of change of'
                                                  'the state vector, in '
                                                  '{}-order').format(
                        self.loopy_opts.order)))
            else:
                logging.warn(
                    'Argument documentation not found for arg {}'.format(x))

        knl_args_doc = '\n'.join(knl_args_doc)
        # these are args passed in (from main, or python)
        # that require initialization, and hence must be passed to mem_init
        input_initialized_args = ', '.join([
            self._get_pass(
                next(x for x in self.mem.arrays if x.name == a),
                include_type=False)
            for a in self.mem.in_arrays if a in self.mem.has_init])
        if input_initialized_args:
            input_initialized_args = ', ' + input_initialized_args

        # and the type included form thereof (for defn's)
        input_initialized_args_defn = ', '.join([
            self._get_pass(next(x for x in self.mem.arrays if x.name == a))
            for a in self.mem.in_arrays if a in self.mem.has_init])
        if input_initialized_args_defn:
            input_initialized_args_defn = ', ' + input_initialized_args_defn
        # and finally the local versions
        input_initialized_args_local = ', '.join([
            self._get_pass(next(x for x in self.mem.arrays if x.name == a),
                           include_type=False,
                           postfix='_local')
            for a in self.mem.in_arrays if a in self.mem.has_init])
        if input_initialized_args_local:
            input_initialized_args_local = ', ' + input_initialized_args_local
        # memory transfers in
        mem_in = self.mem.get_mem_transfers_in()
        # memory transfers out
        mem_out = self.mem.get_mem_transfers_out()
        # memory allocations
        mem_allocs = self.mem.get_mem_allocs()
        # input allocs
        local_allocs = self.mem.get_mem_allocs(True)
        # read args are those that aren't initalized elsewhere
        read_args = ', '.join(['h_' + x + '_local' for x in self.mem.in_arrays
                               if x in ['phi', 'P_arr', 'V_arr']])
        # memory frees
        mem_frees = self.mem.get_mem_frees()
        # input frees
        local_frees = self.mem.get_mem_frees(True)

        # get template
        with open(os.path.join(script_dir, self.lang,
                               'kernel.c.in'), 'r') as file:
            file_src = Template(file.read())

        file_src = self._special_kernel_subs(file_src)

        with filew.get_file(os.path.join(path, self.name + '_main' + utils.file_ext[
                self.lang]), self.lang, use_filter=False) as file:
            file.add_lines(file_src.safe_substitute(
                mem_declares=mem_declares,
                knl_args=knl_args,
                knl_args_doc=knl_args_doc,
                knl_name=self.name,
                input_args=input_args,
                local_input_args=local_input_args,
                input_initialized_args=input_initialized_args,
                input_initialized_args_defn=input_initialized_args_defn,
                input_initialized_args_local=input_initialized_args_local,
                mem_transfers_in=mem_in,
                mem_transfers_out=mem_out,
                mem_allocs=mem_allocs,
                mem_frees=mem_frees,
                read_args=read_args,
                order=self.loopy_opts.order,
                data_filename=data_filename,
                local_allocs=local_allocs,
                local_frees=local_frees
            ))

    def _generate_compiling_program(self, path):
        """
        Needed for some languages (e.g., OpenCL) this may be overriden in
        subclasses to generate a program that compilers the kernel

        Parameters
        ----------
        path : str
            The output path for the compiling program

        Returns
        -------
        None
        """

        pass

    def _generate_wrapping_kernel(self, path):
        """
        Generates a wrapper around the various subkernels in this
        :class:`kernel_generator` (rather than working through loopy's fusion)

        Parameters
        ----------
        path : str
            The output path to write files to

        Returns
        -------
        None
        """

        from loopy.types import AtomicNumpyType, to_loopy_type

        assert all(
            isinstance(x, lp.LoopKernel) or
            next((y for y in self.external_kernels if x.name == y.name), None)
            for x in self.kernels), ('Cannot generate wrapper '
                                     'before calling _make_kernels')

        if self.depends_on:
            # generate wrappers for dependencies
            for x in self.depends_on:
                x._generate_wrapping_kernel(path)

        self.file_prefix = ''
        if self.auto_diff:
            self.file_prefix = 'ad_'

        # first, load the wrapper as a template
        with open(os.path.join(
                script_dir,
                self.lang,
                'wrapping_kernel{}.in'.format(utils.file_ext[self.lang])),
                  'r') as file:
            file_str = file.read()
            file_src = Template(file_str)

        # Find the list of all arguements needed for this kernel
        # this may change in the future

        kernel_data = []
        # need to find mapping of externel kernels to depends
        for x in self.external_kernels:
            knl = next(
                (y for dep in self.depends_on
                 for y in dep.kernels if y.name == x.name), None)
            assert knl, (
                'Cannot find external kernel {} in any dependencies'.format(
                         x.name))
            my_knl_ind = next(
                (i for i, k in enumerate(self.kernels) if x.name == k.name),
                None)
            # now replace
            self.kernels[my_knl_ind] = knl

        # now scan through all our (and externel) kernels
        # and compile the args
        defines = [arg for dummy in self.kernels for arg in dummy.args if
                   not isinstance(arg, lp.TemporaryVariable)]

        # find problem_size
        problem_size = next(x for x in defines if x == p_size)

        # remove other value args
        defines = [x for x in defines if not isinstance(x, lp.ValueArg)]

        # check for dupicates
        nameset = sorted(set(d.name for d in defines))
        for name in nameset:
            same_name = []
            for x in defines:
                if x.name == name and not any(x == y for y in same_name):
                    same_name.append(x)
            if len(same_name) != 1:
                # need to see if differences are resolvable
                atomic = next((x for x in same_name if
                               isinstance(x.dtype, AtomicNumpyType)), None)

                def __raise():
                    raise Exception('Cannot resolve different arguements of '
                                    'same name: {}'.format(', '.join(
                                        str(x) for x in same_name)))

                if atomic is None or len(same_name) > 2:
                    # if we don't have an atomic, or we have multiple different
                    # args of the same name...
                    __raise()

                other = next(x for x in same_name if x != atomic)

                # check that all other properties are the same
                if other != atomic and other.copy(
                        dtype=to_loopy_type(other.dtype, for_atomic=True)) != atomic:
                    __raise()

                # otherwise, they're the same and the only difference is the
                # the atomic.
                # Next, we try to copy all the other kernels with this arg in it
                # with the atomic arg
                for i, knl in enumerate(self.kernels):
                    if other in knl.args:
                        self.kernels[i] = knl.copy(args=[
                            x if x != other else atomic for x in knl.args])

                same_name.remove(other)

            same_name = same_name.pop()
            kernel_data.append(same_name)

        # remove and insert at front
        kernel_data.insert(0, problem_size)

        self.all_arrays = kernel_data[:]
        self.mem.add_arrays(kernel_data)

        def _name_assign(arr):
            if arr.name not in ['P_arr', 'V_arr', 'phi'] and not \
                    isinstance(arr, lp.ValueArg):
                return arr.name + '[{ind}] = 0 {atomic}'.format(
                    ind=', '.join(['0'] * len(arr.shape)),
                    atomic='{atomic}' if isinstance(arr.dtype, AtomicNumpyType)
                           else '')
            return ''

        # generate the kernel definition
        self.vec_width = self.loopy_opts.depth
        if self.vec_width is None:
            self.vec_width = self.loopy_opts.width
        if self.vec_width is None:
            self.vec_width = 0

        inames, _ = self.get_inames(0)

        domains = []
        for iname in ['i'] + inames:
            domains.append('{{[{iname}]: 0 <= {iname} < {size}}}'.format(
                iname=iname,
                size=self.vec_width))

        # create a dummy kernel to get the defn
        knl = lp.make_kernel(domains,
                             '\n'.join(_name_assign(arr)
                                       for arr in kernel_data),
                             kernel_data[:],
                             name=self.name,
                             target=lp_utils.get_target(
                                 self.lang, self.loopy_opts.device)
                             )
        # force vector width
        if self.vec_width != 0:
            ggs = vecwith_fixer(knl.copy(), self.vec_width)
            knl = knl.copy(overridden_get_grid_sizes_for_insn_ids=ggs)
        # get defn
        defn_str = lp_utils.get_header(knl)

        # and finally, generate the kernel code

        preambles = []
        inits = []
        instructions = []
        # split into bodies, preambles, etc.
        for k in self.kernels:
            cgr = lp.generate_code_v2(k)
            # grab preambles
            for _, preamble in cgr.device_preambles:
                if preamble not in preambles:
                    preambles.append(preamble)

            # now scan device program
            assert len(cgr.device_programs) == 1
            cgr = cgr.device_programs[0]
            if isinstance(cgr.ast, cgen.Collection):
                # look for preambles
                for item in cgr.ast.contents:
                    # initializers go in the preamble
                    if isinstance(item, cgen.Initializer):
                        if str(item) not in inits:
                            inits.append(str(item))

                    # blanklines and bodies can be ignored (as they will be added
                    # below)
                    elif not (isinstance(item, cgen.Line)
                              or isinstance(item, cgen.FunctionBody)):
                        raise NotImplementedError(type(item))

            # leave a comment to distinguish the name
            # and put the body in
            instructions.append('// {name}\n{body}\n'.format(
                name=k.name, body=str(cgr.body_ast)))

        # insert barriers if any
        instructions = self.apply_barriers(self.barriers, instructions)

        # join to str
        instructions = '\n'.join(instructions)
        preamble = '\n'.join(preambles + inits)

        file_src = self._special_wrapper_subs(file_src)

        self.filename = os.path.join(
            path,
            self.file_prefix + self.name + utils.file_ext[self.lang])
        # create the file
        with filew.get_file(
                self.filename, self.lang, include_own_header=True) as file:
            instructions = _find_indent(file_str, 'body', instructions)
            preamble = _find_indent(file_str, 'preamble', preamble)
            lines = file_src.safe_substitute(
                defines='',
                preamble=preamble,
                func_define=defn_str[:defn_str.index(';')],
                body=instructions).split('\n')

            if self.auto_diff:
                lines = [x.replace('double', 'adouble') for x in lines]
            file.add_lines(lines)

        # and the header file
        headers = ([lp_utils.get_header(x) + '\n' for x in self.kernels] +
                   [defn_str + utils.line_end[self.lang]])
        with filew.get_header_file(
            os.path.join(path, self.file_prefix + self.name +
                         utils.header_ext[self.lang]), self.lang) as file:

            lines = '\n'.join(headers).split('\n')
            if self.auto_diff:
                file.add_headers('adept.h')
                file.add_lines('using adept::adouble;\n')
                lines = [x.replace('double', 'adouble') for x in lines]
            file.add_lines(lines)

    def remove_unused_temporaries(self, knl):
        """
        Convenience method to remove unused temporary variables from created
        :class:`loopy.LoopKernel`'s
        """
        new_args = []

        exp_knl = lp.expand_subst(knl)

        refd_vars = set(knl.all_params())
        for insn in exp_knl.instructions:
            refd_vars.update(insn.dependency_names())

        from loopy.kernel.array import ArrayBase, FixedStrideArrayDimTag
        from loopy.symbolic import get_dependencies
        from itertools import chain

        def tolerant_get_deps(expr):
            if expr is None or expr is lp.auto:
                return set()
            return get_dependencies(expr)

        for ary in chain(knl.args, six.itervalues(knl.temporary_variables)):
            if isinstance(ary, ArrayBase):
                refd_vars.update(
                        tolerant_get_deps(ary.shape)
                        | tolerant_get_deps(ary.offset))

                for dim_tag in ary.dim_tags:
                    if isinstance(dim_tag, FixedStrideArrayDimTag):
                        refd_vars.update(
                                tolerant_get_deps(dim_tag.stride))

        for arg in knl.temporary_variables:
            if arg in refd_vars:
                new_args.append(arg)

        return knl.copy(temporary_variables={arg: knl.temporary_variables[arg]
                                             for arg in new_args})

    def make_kernel(self, info, target, test_size):
        """
        Convience method to create loopy kernels from kernel_info

        Parameters
        ----------
        info : :class:`knl_info`
            The rate contstant info to generate the kernel from
        target : :class:`loopy.TargetBase`
            The target to generate code for
        test_size : int/str
            The integer (or symbolic) problem size

        Returns
        -------
        knl : :class:`loopy.LoopKernel`
            The generated loopy kernel
        """

        # and the skeleton kernel
        skeleton = self.skeleton[:]

        # convert instructions into a list for convienence
        instructions = info.instructions
        if isinstance(instructions, str):
            instructions = textwrap.dedent(info.instructions)
            instructions = [x for x in instructions.split('\n') if x.strip()]

        # load inames
        our_inames, our_iname_domains = self.get_inames(test_size)
        inames = [info.var_name] + our_inames

        # add map instructions
        instructions = list(info.mapstore.transform_insns) + instructions

        # look for extra inames, ranges
        iname_range = []

        assumptions = info.assumptions[:]

        # find the start index for 'i'
        iname, iname_domain = info.mapstore.get_iname_domain()

        # add to ranges
        iname_range.append(iname_domain)
        iname_range.extend(our_iname_domains)

        assumptions = []
        if isinstance(test_size, str):
            assumptions.extend(self.get_assumptions(test_size))

        for iname, irange in info.extra_inames:
            inames.append(iname)
            iname_range.append(irange)

        # construct the kernel args
        pre_instructions = info.pre_instructions[:]
        post_instructions = info.post_instructions[:]

        def subs_preprocess(key, value):
            # find the instance of ${key} in kernel_str
            result = _find_indent(skeleton, key, value)
            return Template(result).safe_substitute(var_name=info.var_name)

        kernel_str = Template(skeleton).safe_substitute(
            var_name=info.var_name,
            pre=subs_preprocess('${pre}', '\n'.join(pre_instructions)),
            post=subs_preprocess('${post}', '\n'.join(post_instructions)),
            main=subs_preprocess('${main}', '\n'.join(instructions)))

        # finally do extra subs
        if info.extra_subs:
            kernel_str = Template(kernel_str).safe_substitute(
                **info.extra_subs)

        iname_arr = []
        # generate iname strings
        for iname, irange in zip(*(inames, iname_range)):
            iname_arr.append(Template(
                '{[${iname}]:${irange}}').safe_substitute(
                iname=iname,
                irange=irange
            ))

        # get extra mapping data
        extra_kernel_data = [domain(node.iname)[0]
                             for domain, node in
                             six.iteritems(info.mapstore.domain_to_nodes)
                             if not node.is_leaf()]

        extra_kernel_data += self.extra_kernel_data[:]

        # check for duplicate kernel data (e.g. multiple phi arguements)
        kernel_data = []
        for k in info.kernel_data + extra_kernel_data:
            if k not in kernel_data:
                kernel_data.append(k)

        # make the kernel
        knl = lp.make_kernel(iname_arr,
                             kernel_str,
                             kernel_data=kernel_data,
                             name=info.name,
                             target=target,
                             assumptions=' and '.join(assumptions)
                             )
        # fix parameters
        if info.parameters:
            knl = lp.fix_parameters(knl, **info.parameters)
        # prioritize and return
        knl = lp.prioritize_loops(knl, [y for x in inames
                                        for y in x.split(',')])
        # check manglers
        if info.manglers:
            knl = lp.register_function_manglers(knl, info.manglers)

        # check preambles
        if info.preambles:
            # register custom preamble functions
            knl = lp.register_preamble_generators(knl, info.preambles)
            # also register their function manglers
            knl = lp.register_function_manglers(knl, [
                p.get_func_mangler() for p in info.preambles])

        return self.remove_unused_temporaries(knl)

    def apply_specialization(self, loopy_opts, inner_ind, knl, vecspec=None,
                             can_vectorize=True):
        """
        Applies wide / deep vectorization and/or ILP loop unrolling
        to a loopy kernel

        Parameters
        ----------
        loopy_opts : :class:`loopy_options` object
            A object containing all the loopy options to execute
        inner_ind : str
            The inner loop index variable
        knl : :class:`loopy.LoopKernel`
            The kernel to transform
        vecspec : :function:
            An optional specialization function that is applied after
            vectorization to fix hanging loopy issues
        can_vectorize : bool
            If False, cannot be vectorized in the normal manner, hence
            vecspec must be used to vectorize.

        Returns
        -------
        knl : :class:`loopy.LoopKernel`
            The transformed kernel
        """

        # before doing anything, find vec width
        # and split variable
        vec_width = None
        to_split = None
        i_tag = inner_ind
        j_tag = 'j'
        depth = loopy_opts.depth
        width = loopy_opts.width
        if depth:
            to_split = inner_ind
            vec_width = depth
            i_tag += '_outer'
        elif width:
            to_split = 'j'
            vec_width = width
            j_tag += '_outer'
        if not can_vectorize:
            assert vecspec is not None, ('Cannot vectorize a non-vectorizable '
                                         'kernel {} without a specialized '
                                         'vectorization function'.format(
                                            knl.name))

        # if we're splitting
        # apply specified optimizations
        if to_split and can_vectorize:
            # and assign the l0 axis to the correct variable
            knl = lp.split_iname(knl, to_split, vec_width, inner_tag='l.0')

        if utils.can_vectorize_lang[loopy_opts.lang]:
            # tag 'j' as g0, use simple parallelism
            knl = lp.tag_inames(knl, [(j_tag, 'g.0')])

        # if we have a specialization
        if vecspec:
            knl = vecspec(knl)

        if vec_width is not None:
            # finally apply the vector width fix above
            ggs = vecwith_fixer(knl.copy(), vec_width)
            knl = knl.copy(overridden_get_grid_sizes_for_insn_ids=ggs)

        # now do unr / ilp
        if loopy_opts.unr is not None:
            knl = lp.split_iname(knl, i_tag, loopy_opts.unr, inner_tag='unr')
        elif loopy_opts.ilp:
            knl = lp.tag_inames(knl, [(i_tag, 'ilp')])

        return knl


class c_kernel_generator(kernel_generator):
    """
    A C-kernel generator that handles OpenMP parallelization
    """
    def __init__(self, *args, **kw_args):

        super(c_kernel_generator, self).__init__(*args, **kw_args)

        self.extern_defn_template = Template(
            'extern ${type}* ${name}' + utils.line_end[self.lang])
        self.skeleton = """
        ${pre}
        for ${var_name}
            ${main}
        end
        ${post}
        """

        # clear list of inames added to sub kernels, as the OpenMP loop over
        # the states is implemented in the wrapping kernel
        self.inames = []

        # clear list of inames domains added to sub kernels, as the OpenMP loop
        # over the states is implemented in the wrapping kernel
        self.iname_domains = []

        # add 'j' to the list of extra kernel data to be added to subkernels
        self.extra_kernel_data.append(lp.ValueArg('j', dtype=np.int32))

    def get_inames(self, test_size):
        """
        Returns the inames and iname_ranges for subkernels created using
        this generator

        Parameters
        ----------
        test_size : int or str
            In testing, this should be the integer size of the test data
            For production, this should the 'test_size' (or the corresponding)
            for the variable test size passed to the kernel

        Returns
        -------
        inames : list of str
            The string inames to add to created subkernels by default
        iname_domains : list of str
            The iname domains to add to created subkernels by default
        """
        return self.inames, self.iname_domains

    def get_assumptions(self, test_size):
        """
        Returns a list of assumptions on the loop domains
        of generated subkernels

        For the C-kernels, the problem_size is abstracted out into the wrapper
        kernel's OpenMP loop.

        Additionally, there is no concept of a "vector width", hence
        we return an empty assumption set

        Parameters
        ----------
        test_size : int or str
            In testing, this should be the integer size of the test data
            For production, this should the 'test_size' (or the corresponding)
            for the variable test size passed to the kernel

        Returns
        -------

        assumptions : list of str
            List of assumptions to apply to the generated sub kernel
        """

        return []

    def _special_kernel_subs(self, file_src):
        """
        An override of the :method:`kernel_generator._special_wrapping_subs`
        that implements C-specific wrapping kernel arguement passing

        Parameters
        ----------
        file_src : Template
            The kernel source template to substitute into

        Returns:
        new_file_src : Template
            An updated kernel source template to substitute general template
            parameters into
        """

        # and input args

        # these are the args in the kernel defn
        full_kernel_args = ', '.join(self._set_sort(
            [self._get_pass(a, include_type=False, is_host=False)
             for a in self.mem.arrays]))

        return Template(file_src.safe_substitute(
            full_kernel_args=full_kernel_args))


class autodiff_kernel_generator(c_kernel_generator):
    """
    A C-Kernel generator specifically designed to work with the
    autodifferentiation scheme.  Handles adding jacobian, etc.
    """
    def __init__(self, *args, **kw_args):

        super(autodiff_kernel_generator, self).__init__(*args, **kw_args)

        from ..loopy_utils.loopy_utils import AdeptCompiler
        self.compiler = AdeptCompiler()

    def add_jacobian(self, jacobian):
        """
        Adds the jacobian object to the extra kernel data for inclusion in
        generation (to be utilized during the edit / AD process)

        Parameters
        ----------

        jacobian : :class:`loopy.GlobalArg`
            The loopy arguement to add to the method signature

        Returns
        -------
        None
        """

        self.extra_kernel_data.append(jacobian)


class ispc_kernel_generator(kernel_generator):
    def __init__(self, *args, **kw_args):
        super(ispc_kernel_generator, self).__init__(*args, **kw_args)

    # TODO: fill in


class opencl_kernel_generator(kernel_generator):

    """
    An opencl specific kernel generator
    """

    def __init__(self, *args, **kw_args):
        super(opencl_kernel_generator, self).__init__(*args, **kw_args)

        # opencl specific items
        self.set_knl_arg_array_template = Template(
            self.mem.get_check_err_call('clSetKernelArg(kernel,'
                                        '${arg_index}, ${arg_size}, '
                                        '${arg_value})'))
        self.set_knl_arg_value_template = Template(
            self.mem.get_check_err_call('clSetKernelArg(kernel,'
                                        '${arg_index}, ${arg_size}, '
                                        '${arg_value})'))
        self.barrier_templates = {
            'global': 'barrier(CLK_GLOBAL_MEM_FENCE)',
            'local': 'barrier(CLK_LOCAL_MEM_FENCE)'
        }

        # add atomic types to typemap
        from loopy.types import to_loopy_type
        # these don't need to be volatile, as they are on the host side
        self.type_map[to_loopy_type(np.float64, for_atomic=True)] = 'double'
        self.type_map[to_loopy_type(np.int32, for_atomic=True)] = 'int'

    def _special_kernel_subs(self, file_src):
        """
        An override of the :method:`kernel_generator._special_kernel_subs`
        that implements OpenCL specific kernel substitutions

        Parameters
        ----------
        file_src : Template
            The kernel source template to substitute into

        Returns:
        new_file_src : Template
            An updated kernel source template to substitute general template
            parameters into
        """

        # open cl specific
        # vec width
        vec_width = self.vec_width
        if not vec_width:
            # set to default
            vec_width = 1
        # platform
        platform_str = self.loopy_opts.platform.get_info(
            cl.platform_info.VENDOR)
        # build options
        build_options = self.build_options
        # kernel arg setting
        kernel_arg_set = self.get_kernel_arg_setting()
        # kernel list
        kernel_paths = [self.bin_name]
        kernel_paths = ', '.join('"{}"'.format(x)
                                 for x in kernel_paths if x.strip())

        return Template(file_src.safe_substitute(
            vec_width=vec_width,
            platform_str=platform_str,
            build_options=build_options,
            kernel_arg_set=kernel_arg_set,
            kernel_paths=kernel_paths,
            device_type=str(self.loopy_opts.device_type),
            num_source=1  # only 1 program / binary is built
        ))

    def get_kernel_arg_setting(self):
        """
        Needed for OpenCL, this generates the code that sets the kernel args

        Parameters
        ----------
        None

        Returns
        -------
        knl_arg_set_str : str
            The code that sets opencl kernel args
        """

        kernel_arg_sets = []
        for i, arg in enumerate(self.all_arrays):
            if not isinstance(arg, lp.ValueArg):
                kernel_arg_sets.append(
                    self.set_knl_arg_array_template.safe_substitute(
                        arg_index=i,
                        arg_size='sizeof({})'.format('d_' + arg.name),
                        arg_value='&d_' + arg.name)
                )
            else:
                kernel_arg_sets.append(
                    self.set_knl_arg_value_template.safe_substitute(
                        arg_index=i,
                        arg_size='sizeof({})'.format(arg.name),
                        arg_value='&' + arg.name))

        return '\n'.join([
            x + utils.line_end[self.lang] for x in kernel_arg_sets])

    def _generate_compiling_program(self, path):
        """
        Needed for OpenCL, this generates a simple C file that
        compiles and stores the binary OpenCL kernel generated w/ the wrapper

        Parameters
        ----------
        path : str
            The output path to write files to

        Returns
        -------
        None
        """

        assert self.filename, (
            'Cannot generate compiler before wrapping kernel is generated...')
        if self.depends_on:
            assert [x.filename for x in self.depends_on], (
                'Cannot generate compiler before wrapping kernel '
                'for dependencies are generated...')

        self.build_options = ''
        if self.lang == 'opencl':
            with open(os.path.join(script_dir, self.lang,
                                   'opencl_kernel_compiler.c.in'),
                      'r') as file:
                file_str = file.read()
                file_src = Template(file_str)

            # get the platform from the options
            platform_str = self.loopy_opts.platform.get_info(
                cl.platform_info.VENDOR)

            # for the build options, we turn to the siteconf
            self.build_options = ['-I' + x for x in site.CL_INC_DIR + [path]]
            self.build_options.extend(site.CL_FLAGS)
            self.build_options.append('-cl-std=CL{}'.format(site.CL_VERSION))
            self.build_options = ' '.join(self.build_options)

            file_list = [self.filename]
            file_list = ', '.join('"{}"'.format(x) for x in file_list)

            self.bin_name = self.filename[:self.filename.index(
                utils.file_ext[self.lang])] + '.bin'

            with filew.get_file(os.path.join(path, self.name + '_compiler'
                                             + utils.file_ext[self.lang]),
                                self.lang, use_filter=False) as file:
                file.add_lines(file_src.safe_substitute(
                    filenames=file_list,
                    outname=self.bin_name,
                    platform=platform_str,
                    build_options=self.build_options,
                    # compiler expects all source strings
                    num_source=1
                ))

    def apply_barriers(self, barriers, instructions):
        """
        An override of :method:`kernel_generator.apply_barriers` that
        applies synchronization barriers to OpenCL kernels

        Parameters
        ----------
        barriers: list of (i0, i1, type)
            i0: int
                the index of the instruction to insert the barrier after
            i1: int
                the index of the instruction to insert the barrier before,
                i0 + 1 should be equal to i1
            type: ['global', 'local']
                 The type of memory to synchronize, typically global

        instructions: list of str
            The instructions for this kernel

        Returns
        -------

        synchronized_instructions : list of str
            The instruction list with the barriers inserted
        """

        instructions = list(enumerate(instructions))
        for barrier in self.barriers:
            # find insert index (the second barrier ind)
            index = next(ind for ind, inst in enumerate(instructions)
                         if inst[0] == barrier[1])
            # check that we're inserting between the required barriers
            assert barrier[0] == instructions[index - 1][0]
            # and insert
            instructions.insert(index, (-1, self.barrier_templates[barrier[2]]
                                        + utils.line_end[self.lang]))
        # and get rid of indicies
        instructions = [inst[1] for inst in instructions]
        return instructions


class knl_info(object):

    """
    A composite class that contains the various parameters, etc.
    needed to create a simple kernel

    name : str
        The kernel name
    instructions : str or list of str
        The kernel instructions
    mapstore : :class:`array_creator.MapStore`
        The MapStore object containing map domains, indicies, etc.
    pre_instructions : list of str
        The instructions to execute before the inner loop
    post_instructions : list of str
        The instructions to execute after end of inner loop but before end
        of outer loop
    var_name : str
        The inner loop variable
    kernel_data : list of :class:`loopy.ArrayBase`
        The arguements / temporary variables for this kernel
    extra_inames : list of tuple
        A list of (iname, domain) tuples the form the extra loops in this kernel
    assumptions : list of str
        Assumptions to pass to the loopy kernel
    parameters : dict
        Dictionary of parameter values to fix in the loopy kernel
    extra subs : dict
        Dictionary of extra string substitutions to make in kernel generation
    can_vectorize : bool
        If False, the vectorization specializer must be used to vectorize this kernel
    vectorization_specializer : function
        If specified, use this specialization function to fix problems that would
        arise in vectorization
    preambles : :class:`preamble.PreambleGen`
        A list of preamble generators to insert code into loopy / opencl
    """

    def __init__(self, name, instructions, mapstore, pre_instructions=[],
                 post_instructions=[],
                 var_name='i', kernel_data=None,
                 extra_inames=[],
                 assumptions=[], parameters={},
                 extra_subs={},
                 vectorization_specializer=None,
                 can_vectorize=True,
                 manglers=[],
                 preambles=[]):
        self.name = name
        self.instructions = instructions
        self.mapstore = mapstore
        self.pre_instructions = pre_instructions[:]
        self.post_instructions = post_instructions[:]
        self.var_name = var_name
        if isinstance(kernel_data, set):
            kernel_data = list(kernel_data)
        self.kernel_data = kernel_data[:]
        self.extra_inames = extra_inames[:]
        self.assumptions = assumptions[:]
        self.parameters = parameters.copy()
        self.extra_subs = extra_subs
        self.can_vectorize = can_vectorize
        self.vectorization_specializer = vectorization_specializer
        self.manglers = manglers[:]
        self.preambles = preambles[:]


def create_function_mangler(kernel, return_dtypes=()):
    """
    Returns a function mangler to interface loopy kernels with function calls
    to other kernels (e.g. falloff rates from the rate kernel, etc.)

    Parameters
    ----------
    kernel : :class:`loopy.LoopKernel`
        The kernel to create an interface for
    return_dtypes : list :class:`numpy.dtype` returned from the kernel, optional
        Most likely an empty list
    Returns
    -------
    func : :method:`MangleGen`.__call__
        A function that will return a :class:`loopy.kernel.data.CallMangleInfo` to
        interface with the calling :class:`loopy.LoopKernel`
    """
    from ..loopy_utils.preambles_and_manglers import MangleGen

    dtypes = []
    for arg in kernel.args:
        if not isinstance(arg, lp.TemporaryVariable):
            dtypes.append(arg.dtype)
    mg = MangleGen(kernel.name, tuple(dtypes), return_dtypes)
    return mg.__call__


def _find_indent(template_str, key, value):
    """
    Finds and returns a formatted value containing the appropriate
    whitespace to put 'value' in place of 'key' for template_str

    Parameters
    ----------
    template_str : str
        The string to sub into
    key : str
        The key in the template string
    value : str
        The string to format

    Returns
    -------
    formatted_value : str
        The properly indented value
    """

    # find the instance of ${key} in kernel_str
    whitespace = None
    for i, line in enumerate(template_str.split('\n')):
        if key in line:
            # get whitespace
            whitespace = re.match(r'\s*', line).group()
            break
    result = [line if i == 0 else whitespace + line for i, line in
              enumerate(textwrap.dedent(value).splitlines())]
    return '\n'.join(result)


def subs_at_indent(template_str, key, value):
    """
    Substitutes :param:`key` for :param:`value` in :param:`template_str`
    ensuring that the indentation of the value is the same as that of the key
    for all lines present in the value

    Parameters
    ----------
    template_str : str
        The string to sub into
    key : str
        The key in the template string
    value : str
        The string to format

    Returns
    -------
    formatted_value : str
        The formatted string
    """

    return Template(template_str).safe_substitute(**{key: _find_indent(
        template_str, '${{{key}}}'.format(key=key), value)})