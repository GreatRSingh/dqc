import os
import re
import ctypes
from contextlib import contextmanager
from typing import List, Tuple, Optional, Iterator
import torch
import numpy as np
from dqc.utils.datastruct import AtomCGTOBasis

# Terminology:
# * gauss: one gaussian element (multiple gaussian becomes one shell)
# * shell: one contracted basis
# * ao: shell that has been splitted into its components,
#       e.g. p-shell is splitted into 3 components for cartesian (x, y, z)

NDIM = 3
PTR_RINV_ORIG = 4  # from libcint/src/cint_const.h
BLKSIZE = 128  # same as lib/gto/grid_ao_drv.c

# load the libcint
_curpath = os.path.dirname(os.path.abspath(__file__))
_libcint_path = os.path.join(_curpath, "../../submodules/libcint/build/libcint.so")
_libcgto_path = os.path.join(_curpath, "../../lib/libcgto.so")
CINT = ctypes.cdll.LoadLibrary(_libcint_path)
CGTO = ctypes.cdll.LoadLibrary(_libcgto_path)

# Optimizer class
class CINTOpt(ctypes.Structure):
    _fields_ = [
        ('index_xyz_array', ctypes.POINTER(ctypes.POINTER(ctypes.c_int))),
        ('prim_offset', ctypes.POINTER(ctypes.c_int)),
        ('non0ctr', ctypes.POINTER(ctypes.c_int)),
        ('non0idx', ctypes.POINTER(ctypes.POINTER(ctypes.c_int))),
        ('non0coeff', ctypes.POINTER(ctypes.POINTER(ctypes.c_double))),
        ('expij', ctypes.POINTER(ctypes.POINTER(ctypes.c_double))),
        ('rij', ctypes.POINTER(ctypes.POINTER(ctypes.c_double))),
        ('cceij', ctypes.POINTER(ctypes.POINTER(ctypes.c_int))),
        ('tot_prim', ctypes.c_int),
    ]

class _cintoptHandler(ctypes.c_void_p):
    def __del__(self):
        try:
            CGTO.CINTdel_optimizer(ctypes.byref(self))
        except AttributeError:
            pass

class LibcintWrapper(object):
    # this class provides the contracted gaussian integrals
    def __init__(self, atombases: List[AtomCGTOBasis], spherical: bool = True) -> None:
        self._atombases = atombases
        self._spherical = spherical
        self._natoms = len(atombases)

        # the libcint lists
        self._ptr_env = 20
        self._atm_list: List[List[int]] = []
        self._env_list: List[float] = [0.0] * self._ptr_env
        self._bas_list: List[List[int]] = []

        # the list of tensors
        self._allpos: List[torch.Tensor] = []
        self._allalphas: List[torch.Tensor] = []
        self._allcoeffs: List[torch.Tensor] = []
        # offset indicating the starting point of i-th shell in r & (alphas & coeffs)
        # in the flatten list above
        self.shell_to_atom: List[int] = []
        self.shell_to_gauss: List[int] = [0]

        # construct the atom, basis, and env lists
        self._nshells = []
        for i, atombasis in enumerate(atombases):
            # modifying most of the parameters above
            ns = self._add_atom_and_basis(i, atombasis)
            self._nshells.append(ns)
        self.nshells_tot = sum(self._nshells)

        # flatten the params list
        self.allpos_params = torch.cat(self._allpos, dim=0)  # (natom, NDIM)
        self.allalphas_params = torch.cat(self._allalphas, dim=0)  # (ntot_gauss)
        self.allcoeffs_params = torch.cat(self._allcoeffs, dim=0)  # (ntot_gauss)

        # convert the lists to numpy (to make it contiguous, Python list is not contiguous)
        self._atm = np.array(self._atm_list, dtype=np.int32, order="C")
        self._bas = np.array(self._bas_list, dtype=np.int32, order="C")
        self._env = np.array(self._env_list, dtype=np.float64, order="C")

        # get the c-pointer of the numpy array
        self.atm_ctypes = self._atm.ctypes.data_as(ctypes.c_void_p)
        self.bas_ctypes = self._bas.ctypes.data_as(ctypes.c_void_p)
        self.env_ctypes = self._env.ctypes.data_as(ctypes.c_void_p)
        self.natm_ctypes = ctypes.c_int(self._atm.shape[0])
        self.nbas_ctypes = ctypes.c_int(self._bas.shape[0])

        # get dtype and device for torch's tensors
        self.dtype = atombases[0].bases[0].alphas.dtype
        self.device = atombases[0].bases[0].alphas.device

        # get the size of the contracted gaussian
        shell_to_aoloc = [0]
        shell_to_nao = []
        for i in range(self.nshells_tot):
            nbasiscontr = self._nbasiscontr(i)
            shell_to_nao.append(nbasiscontr)
            shell_to_aoloc.append(shell_to_aoloc[-1] + nbasiscontr)
        self.shell_to_aoloc = np.array(shell_to_aoloc, dtype=np.int32)
        self.nao_tot = self.shell_to_aoloc[-1]

        # get the mapping from ao to atom index
        self.ao_to_atom = torch.zeros((self.nao_tot,), dtype=torch.long)
        for i in range(self.nshells_tot):
            idx = self.shell_to_aoloc[i]
            self.ao_to_atom[idx: idx + shell_to_nao[i]] = self.shell_to_atom[i]

    def _add_atom_and_basis(self, iatom: int, atombasis: AtomCGTOBasis) -> int:
        # construct the atom first
        assert atombasis.pos.numel() == NDIM, "Please report this bug in Github"
        #                charge           ptr_coord       nucl model (unused for standard nucl model)
        self._atm_list.append([atombasis.atomz, self._ptr_env, 1, self._ptr_env + NDIM, 0, 0])
        self._env_list.extend(atombasis.pos)
        self._ptr_env += NDIM
        self._env_list.extend([0.0])
        self._ptr_env += 1

        # then construct the basis
        for basis in atombasis.bases:
            assert basis.alphas.shape == basis.coeffs.shape and basis.alphas.ndim == 1,\
                "Please report this bug in Github"

            normcoeff = self._normalize_basis(basis.alphas, basis.coeffs, basis.angmom)

            ngauss = len(basis.alphas)
            #                      iatom, angmom,       ngauss, ncontr, kappa, ptr_exp
            self._bas_list.append([iatom, basis.angmom, ngauss, 1, 0, self._ptr_env,
                                   # ptr_coeffs,           unused
                                   self._ptr_env + ngauss, 0])
            self._env_list.extend(basis.alphas)
            self._env_list.extend(normcoeff)
            self._ptr_env += 2 * ngauss

            # add the basis coeffs and alphas to the flat list and update the offset
            self._allalphas.append(basis.alphas)
            self._allcoeffs.append(normcoeff)
            self.shell_to_gauss.append(self.shell_to_gauss[-1] + ngauss)
            self.shell_to_atom.append(iatom)

        # add the atom position
        self._allpos.append(atombasis.pos.unsqueeze(0))
        return len(atombasis.bases)

    def _normalize_basis(self, alphas: torch.Tensor, coeffs: torch.Tensor,
                         angmom: int) -> torch.Tensor:
        # the normalization is obtained from CINTgto_norm from
        # libcint/src/misc.c, or
        # https://github.com/sunqm/libcint/blob/b8594f1d27c3dad9034984a2a5befb9d607d4932/src/misc.c#L80

        # precomputed factor:
        # 2 ** (2 * angmom + 3) * factorial(angmom + 1) * / \
        # (factorial(angmom * 2 + 2) * np.sqrt(np.pi)))
        factor = [
            2.256758334191025,  # 0
            1.5045055561273502,  # 1
            0.6018022224509401,  # 2
            0.17194349212884005,  # 3
            0.03820966491752001,  # 4
            0.006947211803185456,  # 5
            0.0010688018158746854,  # 6
        ]
        return coeffs * torch.sqrt(factor[angmom] * (2 * alphas) ** (angmom + 1.5))

    @property
    def ao_loc(self):
        return self.shell_to_aoloc

    def overlap(self) -> torch.Tensor:
        return self._int1e("ovlp")

    def kinetic(self) -> torch.Tensor:
        return self._int1e("kin")

    def nuclattr(self) -> torch.Tensor:
        return self._int1e("nuc", True)

    def elrep(self) -> torch.Tensor:
        return self._int2e()

    def eval_gto(self, rgrid: torch.Tensor) -> torch.Tensor:
        # rgrid: (ngrid, ndim)
        # return: (nao, ngrid)
        return self._evalgto("", rgrid)

    def eval_gradgto(self, rgrid: torch.Tensor) -> torch.Tensor:
        # rgrid: (ngrid, ndim)
        # return: (ndim, nao, ngrid)
        return self._evalgto("ip", rgrid)

    def eval_laplgto(self, rgrid: torch.Tensor) -> torch.Tensor:
        # rgrid: (ngrid, ndim)
        # return: (nao, ngrid)
        return self._evalgto("lapl", rgrid)

    ################ integrals to construct the operator ################
    def _int1e(self, shortname: str, nuc: bool = False) -> torch.Tensor:
        # one electron integral (overlap, kinetic, and nuclear attraction)

        shape = (self.nao_tot, self.nao_tot)
        res = torch.empty(shape, dtype=self.dtype, device=self.device)
        for i1 in range(self.nshells_tot):
            c1, a1, r1 = self._get_params(i1)
            slice1 = self._get_matrix_index(i1)

            for i2 in range(i1, self.nshells_tot):
                # get the parameters for backward propagation
                c2, a2, r2 = self._get_params(i2)
                slice2 = self._get_matrix_index(i2)

                if nuc:
                    # computing the nuclear attraction operator per atom
                    # so that the gradient w.r.t. atom's position can be computed
                    mat = torch.tensor(0.0, dtype=self.dtype, device=self.device)
                    for ia in range(self._natoms):

                        # with self._coord_to_centre_on_atom(ia):
                        #     z = float(self._atm[ia, 0])
                        #     mat = mat - z * _Int1eFunction.apply(
                        #         c1, a1, r1 - atompos,
                        #         c2, a2, r2 - atompos,
                        #         self, "rinv", i1, i2)

                        with self._all_atomz_are_zero_except(ia):
                            atompos = self.allpos_params[ia]
                            mat = mat + _Int1eFunction.apply(
                                c1, a1, r1,
                                c2, a2, r2,
                                atompos,
                                self, shortname, i1, i2)

                    # # debugging code
                    # mat2 = _Int1eFunction.apply(
                    #     c1, a1, r1, c2, a2, r2,
                    #     self.allpos_params,
                    #     self, shortname, i1, i2)
                    # assert torch.allclose(mat, mat2)

                else:
                    mat = _Int1eFunction.apply(
                        c1, a1, r1, c2, a2, r2,
                        self.allpos_params,
                        self, shortname, i1, i2)

                # apply symmetry
                res[slice1, slice2] = mat
                res[slice2, slice1] = mat.transpose(-2, -1)
        return res

    def _int2e(self) -> torch.Tensor:
        return self.calc_all_int2e_internal("")

    @contextmanager
    def _all_atomz_are_zero_except(self, ia: int) -> Iterator:
        try:
            _atm_backup = self._atm[:, 0].copy()
            self._atm[:, 0] = 0
            self._atm[ia, 0] = _atm_backup[ia]
            yield
        finally:
            self._atm[:, 0] = _atm_backup[:]

    @contextmanager
    def _coord_to_centre_on_atom(self, ia: int) -> Iterator:
        try:
            # set the coordinates to centre on atoms ia
            idx = self._atm[ia, 1]
            atompos = self._env[idx: idx + NDIM]
            self._env[PTR_RINV_ORIG: PTR_RINV_ORIG + NDIM] = atompos
            yield
        finally:
            # restore the central coordinate into 0.0
            self._env[PTR_RINV_ORIG: PTR_RINV_ORIG + NDIM] = 0.0

    def _get_params(self, sh1: int) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        # getting the parameters of the sh1-th shell (i.e. coeffs, alphas, and pos)
        ib1l = self.shell_to_gauss[sh1]
        ib1u = self.shell_to_gauss[sh1 + 1]
        c1 = self.allcoeffs_params[ib1l:ib1u]
        a1 = self.allalphas_params[ib1l:ib1u]
        r1 = self.allpos_params[self.shell_to_atom[sh1]]
        return c1, a1, r1

    def _get_matrix_index(self, sh1: int) -> slice:
        # getting the lower and upper indices of the sh1-th shell in the contracted
        # integral matrix
        return slice(self.shell_to_aoloc[sh1], self.shell_to_aoloc[sh1 + 1], None)

    ################ one electron integral for a basis pair ################
    def calc_integral_1e_internal(self, shortname: str, sh1: int, sh2: int) -> torch.Tensor:
        # calculate the one-electron integral (type depends on the shortname)
        # for basis in shell sh1 & sh2 using libcint

        # NOTE: this function should only be called from this file only.
        # it does not propagate gradient.

        # check if the operation needs to be flipped (e.g. "nucip" flipped to be
        # "ipnuc")
        shortname, toflip = self._is_int1e_need_flip(shortname)
        toflip = toflip and (sh1 != sh2)
        if toflip:
            sh1, sh2 = sh2, sh1

        opname = self._get_int1e_name(shortname)
        operator = getattr(CINT, opname)
        outshape = self._get_int1e_outshape(shortname, sh1, sh2)

        c_shls = (ctypes.c_int * 2)(sh1, sh2)
        out = np.empty(outshape, dtype=np.float64)
        out_ctypes = out.ctypes.data_as(ctypes.c_void_p)

        # calculate the integral
        operator.restype = ctypes.c_double
        operator(out_ctypes, c_shls, self.atm_ctypes, self.natm_ctypes,
                 self.bas_ctypes, self.nbas_ctypes, self.env_ctypes)

        out_tensor = torch.tensor(out, dtype=self.dtype, device=self.device)

        # flip the last two dimensions because the output of libcint is following
        # fortran memory order, but we want to keep the spatial dimension as
        # the first dimension
        out_tensor = out_tensor.transpose(-2, -1)
        if toflip:
            out_tensor = out_tensor.transpose(-2, -1)
        return out_tensor

    def get_int1e_deriv_shortname(self, opshortname: str, derivmode: str) -> str:
        if derivmode == "r1":
            return "ip%s" % opshortname
        elif derivmode == "r2":
            return "%sip" % opshortname
        else:
            raise RuntimeError("Unknown derivmode: %s" % derivmode)

    def _is_int1e_need_flip(self, shortname: str) -> Tuple[str, bool]:
        # check if the integral needs to be flipped (e.g. "nucip" need to be
        # flipped to "ipnuc" to access the integral from libcint)

        # count the number of "ip" at the beginning and end of the string
        n_ip_start = len(re.findall(r"^(?:ip)*(?:ip)?", shortname)[0]) // 2
        n_ip_end = len(re.findall(r"(?:ip)?(?:ip)*$", shortname)[0]) // 2
        flip = n_ip_end > n_ip_start
        if flip:
            sname = shortname[2 * n_ip_start: -2 * n_ip_end]
            shortname = ("ip" * n_ip_end) + sname + ("ip" * n_ip_start)
        return shortname, flip

    def _get_int1e_name(self, shortname: str):
        # get the full name of the integral 1 electron to be called in libcint
        suffix = "sph" if self._spherical else "cart"
        return "cint1e_%s_%s" % (shortname, suffix)

    def _get_int1e_outshape(self, shortname: str, sh1: int, sh2: int) -> List[int]:
        n_ip_start = len(re.findall(r"^(?:ip)*(?:ip)?", shortname)[0]) // 2
        n_ip_end = len(re.findall(r"(?:ip)?(?:ip)*$", shortname)[0]) // 2
        n_ip = n_ip_start + n_ip_end

        # note that sh2 and sh1 is reversed because the output of libcint
        # is following fortran order
        return ([NDIM] * n_ip) + [self._nbasiscontr(sh2), self._nbasiscontr(sh1)]

    ################ two electrons integral for all basis pairs ################
    def calc_all_int2e_internal(self, shortname: str) -> torch.Tensor:
        # calculation of all 2-electron integrals
        # no gradient is propagated

        opname = self._get_all_int2e_name(shortname)
        operator = getattr(CINT, opname)
        optimizer = self._get_all_int2e_optimizer(opname)

        # prepare the output
        outshape, ncomp = self._get_all_int2e_outshape(shortname)
        out = np.empty(outshape, dtype=np.float64)

        drv = CGTO.GTOnr2e_fill_drv
        fill = CGTO.GTOnr2e_fill_s1
        prescreen = ctypes.POINTER(ctypes.c_void_p)()
        shls_slice = (0, self.nshells_tot) * 4
        drv(operator, fill, prescreen,
            out.ctypes.data_as(ctypes.c_void_p),
            ctypes.c_int(ncomp),
            (ctypes.c_int * 8)(*shls_slice),
            self.shell_to_aoloc.ctypes.data_as(ctypes.c_void_p),
            optimizer,
            self.atm_ctypes, self.natm_ctypes,
            self.bas_ctypes, self.nbas_ctypes,
            self.env_ctypes)

        out_tensor = torch.as_tensor(out, dtype=self.dtype, device=self.device)
        return out_tensor

    def _get_all_int2e_name(self, shortname: str) -> str:
        suffix = ("_" + shortname) if shortname != "" else shortname
        cartsph = "sph" if self._spherical else "cart"
        return "int2e%s_%s" % (suffix, cartsph)

    def _get_all_int2e_optimizer(self, opname: str) -> ctypes.c_void_p:
        # setup the optimizer
        cintopt = ctypes.POINTER(ctypes.c_void_p)()
        copt = getattr(CINT, "int2e_optimizer")
        copt(ctypes.byref(cintopt),
             self.atm_ctypes, self.natm_ctypes,
             self.bas_ctypes, self.nbas_ctypes, self.env_ctypes)
        opt = ctypes.cast(cintopt, _cintoptHandler)
        return opt

    def _get_all_int2e_outshape(self, shortname: str) -> Tuple[Tuple[int, ...], int]:
        n_ip_start = len(re.findall(r"^(?:ip)*(?:ip)?", shortname)[0]) // 2
        n_ip_end = len(re.findall(r"(?:ip)?(?:ip)*$", shortname)[0]) // 2
        n_ip = n_ip_start + n_ip_end
        outshape = (NDIM, ) * n_ip + (self.nao_tot, ) * 4
        ncomp = NDIM ** n_ip
        return outshape, ncomp

    ################ evaluation of gto orbitals ################
    def _evalgto(self, shortname: str, rgrid: torch.Tensor) -> torch.Tensor:
        # expand ao_to_atom to have shape of (nao, ndim)
        ao_to_atom = self.ao_to_atom.unsqueeze(-1).expand(-1, NDIM)

        # rgrid: (ngrid, ndim)
        return _EvalGTO.apply(
            # tensors
            self.allalphas_params,
            self.allcoeffs_params,
            self.allpos_params,
            rgrid,

            # nontensors or int tensors
            ao_to_atom,
            self,
            shortname)

    def _get_evalgto_opname(self, shortname: str) -> str:
        sname = ("_" + shortname) if (shortname != "") else ""
        suffix = "_sph" if self._spherical else "_cart"
        return "GTOval%s%s" % (sname, suffix)

    def _get_evalgto_outshape(self, shortname: str, nao: int, ngrid: int) -> List[int]:
        # count "ip" only at the beginning
        n_ip = len(re.findall(r"^(?:ip)*(?:ip)?", shortname)[0]) // 2
        return ([NDIM] * n_ip) + [nao, ngrid]

    def _get_evalgto_derivname(self, shortname: str, derivmode: str):
        if derivmode == "r":
            return "ip%s" % shortname
        else:
            raise RuntimeError("Unknown derivmode: %s" % derivmode)

    def eval_gto_internal(self, shortname: str, rgrid: torch.Tensor) -> torch.Tensor:
        # NOTE: this method do not propagate gradient and should only be used
        # in this file only

        # rgrid: (ngrid, ndim)
        # returns: (*, nao, ngrid)

        ngrid = rgrid.shape[0]
        nshells = self.nshells_tot
        nao = self.nao_tot
        opname = self._get_evalgto_opname(shortname)
        outshape = self._get_evalgto_outshape(shortname, nao, ngrid)

        out = np.empty(outshape, dtype=np.float64)
        non0tab = np.ones(((ngrid + BLKSIZE - 1) // BLKSIZE, nshells),
                          dtype=np.int8)

        # TODO: check if we need to transpose it first?
        rgrid = rgrid.contiguous()
        coords = np.asarray(rgrid, dtype=np.float64, order='F')
        ao_loc = np.asarray(self.shell_to_aoloc, dtype=np.int32)

        c_shls = (ctypes.c_int * 2)(0, nshells)
        c_ngrid = ctypes.c_int(ngrid)

        # evaluate the orbital
        operator = getattr(CGTO, opname)
        operator.restype = ctypes.c_double
        operator(c_ngrid, c_shls,
                 ao_loc.ctypes.data_as(ctypes.c_void_p),
                 out.ctypes.data_as(ctypes.c_void_p),
                 coords.ctypes.data_as(ctypes.c_void_p),
                 non0tab.ctypes.data_as(ctypes.c_void_p),
                 self.atm_ctypes, self.natm_ctypes,
                 self.bas_ctypes, self.nbas_ctypes,
                 self.env_ctypes)

        out = torch.tensor(out, dtype=self.dtype, device=self.device)
        return out

    ################ misc functions ################
    def _nbasiscontr(self, sh: int) -> int:
        if self._spherical:
            op = CINT.CINTcgto_spheric
        else:
            op = CINT.CINTcgto_cart
        return op(ctypes.c_int(sh), self.bas_ctypes)

############### autograd function ###############
class _Int1eFunction(torch.autograd.Function):
    # wrapper class to provide the gradient of the one-e integrals
    @staticmethod
    def forward(ctx,  # type: ignore
                c1: torch.Tensor, a1: torch.Tensor, r1: torch.Tensor,
                c2: torch.Tensor, a2: torch.Tensor, r2: torch.Tensor,
                ratom: torch.Tensor,
                wrapper: LibcintWrapper, opshortname: str, sh1: int, sh2: int) -> \
            torch.Tensor:  # type: ignore
        # c_: (ngauss,)
        # a_: (ngauss,)
        # r_: (NDIM,)
        # ratom: (NDIM,)
        # they are not used in forward, but present in the argument so that
        # the gradient can be propagated
        out_tensor = wrapper.calc_integral_1e_internal(opshortname, sh1, sh2)
        ctx.save_for_backward(c1, a1, r1, c2, a2, r2, ratom)
        ctx.other_info = (wrapper, sh1, sh2, opshortname)
        return out_tensor  # (*outshape)

    @staticmethod
    def backward(ctx, grad_out: torch.Tensor) -> Tuple[Optional[torch.Tensor], ...]:  # type: ignore
        # grad_out: (*outshape)
        c1, a1, r1, c2, a2, r2, ratom = ctx.saved_tensors
        wrapper, sh1, sh2, opshortname = ctx.other_info
        nuc_int = "nuc" in opshortname
        grad_ratom_reqgrad = nuc_int and ratom.requires_grad

        # TODO: to be completed (???)
        grad_c1: Optional[torch.Tensor] = None
        grad_a1: Optional[torch.Tensor] = None
        grad_c2: Optional[torch.Tensor] = None
        grad_a2: Optional[torch.Tensor] = None

        grad_r1: Optional[torch.Tensor] = None
        if grad_ratom_reqgrad or r1.requires_grad:
            opsname = wrapper.get_int1e_deriv_shortname(opshortname, "r1")
            doutdr1 = _Int1eFunction.apply(*ctx.saved_tensors, wrapper, opsname,
                                           sh1, sh2)  # (NDIM, *outshape)
            # negative because the integral calculates the nabla w.r.t. the
            # spatial coordinate, not the basis central position
            grad_r1 = -(grad_out * doutdr1).reshape(NDIM, -1).sum(dim=-1)

        grad_r2: Optional[torch.Tensor] = None
        if grad_ratom_reqgrad or r2.requires_grad:
            opsname = wrapper.get_int1e_deriv_shortname(opshortname, "r2")
            doutdr2 = _Int1eFunction.apply(*ctx.saved_tensors, wrapper, opsname,
                                           sh1, sh2)
            grad_r2 = -(grad_out * doutdr2).reshape(NDIM, -1).sum(dim=-1)

        grad_ratom: Optional[torch.Tensor] = None
        if grad_ratom_reqgrad:
            grad_ratom = -(grad_r1 + grad_r2)  # type: ignore

        return grad_c1, grad_a1, grad_r1, grad_c2, grad_a2, grad_r2, grad_ratom, \
            None, None, None, None

class _EvalGTO(torch.autograd.Function):
    @staticmethod
    def forward(ctx,  # type: ignore
                # tensors not used in calculating the forward, but required
                # for the backward propagation
                alphas: torch.Tensor,  # (ngauss_tot)
                coeffs: torch.Tensor,  # (ngauss_tot)
                pos: torch.Tensor,  # (natom, ndim)

                # tensors used in forward
                rgrid: torch.Tensor,  # (ngrid, ndim)

                # other non-tensor info
                ao_to_atom: torch.Tensor,  # int tensor (nao, ndim)
                wrapper: LibcintWrapper,
                shortname: str) -> torch.Tensor:

        res = wrapper.eval_gto_internal(shortname, rgrid)  # (*, nao, ngrid)
        ctx.save_for_backward(alphas, coeffs, pos, rgrid)
        ctx.other_info = (ao_to_atom, wrapper, shortname)
        return res

    @staticmethod
    def backward(ctx, grad_res: torch.Tensor) -> Tuple[Optional[torch.Tensor], ...]:  # type: ignore
        # grad_res: (*, nao, ngrid)
        ao_to_atom, wrapper, shortname = ctx.other_info
        alphas, coeffs, pos, rgrid = ctx.saved_tensors

        # TODO: implement the gradient w.r.t. alphas and coeffs
        grad_alphas = None
        grad_coeffs = None

        # calculate the gradient w.r.t. basis' pos and rgrid
        grad_pos = None
        grad_rgrid = None
        if rgrid.requires_grad or pos.requires_grad:
            opsname = wrapper._get_evalgto_derivname(shortname, "r")
            dresdr = _EvalGTO.apply(*ctx.saved_tensors,
                                    ao_to_atom, wrapper, opsname)  # (ndim, *, nao, ngrid)
            grad_r = dresdr * grad_res  # (ndim, *, nao, ngrid)

            if rgrid.requires_grad:
                grad_rgrid = grad_r.reshape(dresdr.shape[0], -1, dresdr.shape[-1])
                grad_rgrid = grad_rgrid.sum(dim=1).transpose(-2, -1)  # (ngrid, ndim)

            if pos.requires_grad:
                grad_rao = torch.movedim(grad_r, -2, 0)  # (nao, ndim, *, ngrid)
                grad_rao = -grad_rao.reshape(*grad_rao.shape[:2], -1).sum(dim=-1)  # (nao, ndim)
                grad_pos = torch.zeros_like(pos)  # (natom, ndim)
                grad_pos.scatter_add_(dim=0, index=ao_to_atom, src=grad_rao)

        return grad_alphas, grad_coeffs, grad_pos, grad_rgrid, \
            None, None, None, None, None
