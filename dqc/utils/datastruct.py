from __future__ import annotations
import torch
from dataclasses import dataclass
from typing import Optional, Union, List, TypeVar, Generic
from dqc.utils.misc import gaussian_int

__all__ = ["CGTOBasis", "AtomCGTOBasis", "ValGrad", "ZType", "is_z_float",
           "BasisInpType", "DensityFitInfo", "SpinParam"]

T = TypeVar('T')

# type of the atom Z
ZType = Union[int, float, torch.Tensor]

def is_z_float(a: ZType):
    # returns if the given z-type is a floating point
    if isinstance(a, torch.Tensor):
        return a.is_floating_point()
    else:
        return isinstance(a, float)

@dataclass
class CGTOBasis:
    angmom: int
    alphas: torch.Tensor  # (nbasis,)
    coeffs: torch.Tensor  # (nbasis,)
    normalized: bool = False

    def wfnormalize_(self) -> CGTOBasis:
        # wavefunction normalization
        # the normalization is obtained from CINTgto_norm from
        # libcint/src/misc.c, or
        # https://github.com/sunqm/libcint/blob/b8594f1d27c3dad9034984a2a5befb9d607d4932/src/misc.c#L80

        # Please note that the square of normalized wavefunctions do not integrate
        # to 1, but e.g. for s: 4*pi, p: (4*pi/3)

        # if the basis has been normalized before, then do nothing
        if self.normalized:
            return self

        coeffs = self.coeffs

        # normalize to have individual gaussian integral to be 1 (if coeff is 1)
        coeffs = coeffs / torch.sqrt(gaussian_int(2 * self.angmom + 2, 2 * self.alphas))

        # normalize the coefficients in the basis (because some basis such as
        # def2-svp-jkfit is not normalized to have 1 in overlap)
        ee = self.alphas.unsqueeze(-1) + self.alphas.unsqueeze(-2)  # (ngauss, ngauss)
        ee = gaussian_int(2 * self.angmom + 2, ee)
        s1 = 1 / torch.sqrt(torch.einsum("a,ab,b", coeffs, ee, coeffs))
        coeffs = coeffs * s1

        self.coeffs = coeffs
        self.normalized = True
        return self

@dataclass
class AtomCGTOBasis:
    atomz: ZType
    bases: List[CGTOBasis]
    pos: torch.Tensor  # (ndim,)

# input basis type
BasisInpType = Union[str, List[CGTOBasis], List[str], List[List[CGTOBasis]]]

@dataclass
class DensityFitInfo:
    method: str
    auxbases: List[AtomCGTOBasis]

@dataclass
class SpinParam(Generic[T]):
    u: T
    d: T

@dataclass
class ValGrad:
    value: torch.Tensor  # torch.Tensor of the value in the grid
    grad: Optional[torch.Tensor] = None  # torch.Tensor representing (gradx, grady, gradz) with shape
    # ``(..., 3)``
    lapl: Optional[torch.Tensor] = None  # torch.Tensor of the laplace of the value

    def __add__(self, b: ValGrad) -> ValGrad:
        return ValGrad(
            value=self.value + b.value,
            grad=self.grad + b.grad if self.grad is not None else None,
            lapl=self.lapl + b.lapl if self.lapl is not None else None,
        )

    def __mul__(self, f: Union[float, int, torch.Tensor]) -> ValGrad:
        if isinstance(f, torch.Tensor):
            assert f.numel() == 1, "ValGrad multiplication with tensor can only be done with 1-element tensor"

        return ValGrad(
            value=self.value * f,
            grad=self.grad * f if self.grad is not None else None,
            lapl=self.lapl * f if self.lapl is not None else None,
        )
