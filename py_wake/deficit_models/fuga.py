import os
import struct
from numpy import newaxis as na
import numpy as np
from py_wake.deficit_models.deficit_model import DeficitModel
from py_wake.superposition_models import LinearSum
from py_wake.wind_farm_models.engineering_models import PropagateDownwind, All2AllIterative
from py_wake.rotor_avg_models.rotor_avg_model import RotorCenter
from py_wake.tests.test_files import tfp
from py_wake.utils.grid_interpolator import GridInterpolator
from pathlib import Path


class FugaUtils():
    def __init__(self, path, on_mismatch='raise'):
        """
        Parameters
        ----------
        path : string
            Path to folder containing 'CaseData.bin', input parameter file (*.par) and loop-up tables
        on_mismatch : {'raise', 'casedata','input_par'}
            Determines how to handle mismatch between info from CaseData.in and input.par.
            If 'raise' a ValueError exception is raised in case of mismatch\n
            If 'casedata', the values from CaseData.bin is used\n
            If 'input_par' the values from the input parameter file (*.par) is used
        """
        self.path = path

        with open(self.path + 'CaseData.bin', 'rb') as fid:
            case_name = struct.unpack('127s', fid.read(127))[0]  # @UnusedVariable
            self.r = struct.unpack('d', fid.read(8))[0]  # @UnusedVariable
            self.zHub = struct.unpack('d', fid.read(8))[0]
            self.low_level = struct.unpack('I', fid.read(4))[0]
            self.high_level = struct.unpack('I', fid.read(4))[0]
            self.z0 = struct.unpack('d', fid.read(8))[0]
            zi = struct.unpack('d', fid.read(8))[0]  # @UnusedVariable
            self.ds = struct.unpack('d', fid.read(8))[0]
            closure = struct.unpack('I', fid.read(4))[0]
            if os.path.getsize(self.path + 'CaseData.bin') == 187:
                self.zeta0 = struct.unpack('d', fid.read(8))[0]
            else:
                #                 with open(path + 'CaseData.bin', 'rb') as fid2:
                #                     info = fid2.read(127).decode()
                #                 zeta0 = float(info[info.index('Zeta0'):].replace("Zeta0=", ""))
                if 'Zeta0' in self.path:
                    self.zeta0 = float(self.path[self.path.index('Zeta0'):].replace("Zeta0=", "").replace("/", ""))

        f = [f for f in os.listdir(self.path) if f.endswith('.par')][0]
        lines = Path(self.path + f).read_text().split("\n")

        self.prefix = lines[0].strip()
        self.nx, self.ny = map(int, lines[2:4])
        self.dx, self.dy = map(float, lines[4:6])  # @UnusedVariable
        self.sigmax, self.sigmay = map(float, lines[6:8])  # @UnusedVariable

        def set_Value(n, v):
            if on_mismatch == 'raise' and getattr(self, n) != v:
                raise ValueError("Mismatch between CaseData.bin and %s: %s %s!=%s" % (f, n, getattr(self, n), v))
            elif on_mismatch == 'input_par':
                setattr(self, n, v)

        set_Value('low_level', int(lines[11]))
        set_Value('high_level', int(lines[12]))
        set_Value('z0', float(lines[8]))  # roughness level
        set_Value('zHub', float(lines[10]))  # hub height
        self.nx0 = self.nx // 4
        self.ny0 = self.ny // 2

        self.x = np.arange(-self.nx0, self.nx * 3 / 4) * self.dx  # rotor is located 1/4 downstream
        self.y = np.arange(self.ny // 2) * self.dy
        self.zlevels = np.arange(self.low_level, self.high_level + 1)

        if self.low_level == self.high_level == 9999:
            self.z = [self.zHub]
        else:
            self.z = self.z0 * np.exp(self.zlevels * self.ds)

    def mirror(self, x, anti_symmetric=False):
        x = np.asarray(x)
        return np.concatenate([((1, -1)[anti_symmetric]) * x[::-1], x[1:]])

    def load_luts(self, UVLT=['UL', 'UT', 'VL', 'VT'], zlevels=None):
        return np.array([[np.fromfile(self.path + self.prefix + '%04d%s.dat' % (j, uvlt), np.dtype('<f'), -1)
                          for j in (zlevels or self.zlevels)] for uvlt in UVLT])


class FugaDeficit(DeficitModel, FugaUtils):
    ams = 5
    invL = 0
    args4deficit = ['WS_ilk', 'WS_eff_ilk', 'dw_ijlk', 'hcw_ijlk', 'dh_ijl', 'h_il', 'ct_ilk', 'D_src_il']

    def __init__(self, LUT_path=tfp + 'fuga/2MW/Z0=0.03000000Zi=00401Zeta0=0.00E+0/', remove_wriggles=False):
        """
        Parameters
        ----------
        LUT_path : str
            Path to folder containing 'CaseData.bin', input parameter file (*.par) and loop-up tables
        remove_wriggles : bool
            The current Fuga loop-up tables have significan wriggles.
            If True, all deficit values after the first zero crossing (when going from the center line
            and out in the lateral direction) is set to zero.
            This means that all speed-up regions are also removed
        """

        FugaUtils.__init__(self, LUT_path, on_mismatch='input_par')
        self.remove_wriggles = remove_wriggles
        self.lut_interpolator = LUTInterpolator(*self.load())

    def load(self):

        def psim(zeta):
            return self.ams * zeta

        if not self.zeta0 >= 0:  # pragma: no cover
            # See Colonel.u2b.psim
            raise NotImplementedError
        factor = 1 / (1 - (psim(self.zHub * self.invL) - psim(self.zeta0)) / np.log(self.zHub / self.z0))

        mdu = self.load_luts(['UL'])[0]

        du = -np.array(mdu, dtype=np.float32).reshape((len(mdu), self.ny // 2, self.nx)) * factor

        if self.remove_wriggles:
            # remove all positive and negative deficits after first zero crossing in lateral direction
            du *= (np.cumsum(du < 0, 1) == 0)

        # smooth edges to zero
        n = 250
        du[:, :, :n] = du[:, :, n][:, :, na] * np.arange(n) / n
        du[:, :, -n:] = du[:, :, -n][:, :, na] * np.arange(n)[::-1] / n
        n = 50
        du[:, -n:, :] = du[:, -n, :][:, na, :] * np.arange(n)[::-1][na, :, na] / n

        return self.x, self.y, self.z, du

    def interpolate(self, x, y, z):
        # self.grid_interplator(np.array([zyx.flatten() for zyx in [z, y, x]]).T, check_bounds=False).reshape(x.shape)
        return self.lut_interpolator((x, y, z))

    def _calc_layout_terms(self, dw_ijlk, hcw_ijlk, h_il, dh_ijl, D_src_il, **_):

        self.mdu_ijlk = self.interpolate(dw_ijlk, np.abs(hcw_ijlk), (h_il[:, na] + dh_ijl)[:, :, :, na]) * \
            ~((dw_ijlk == 0) & (hcw_ijlk <= D_src_il[:, na, :, na])  # avoid wake on itself
              )

    def calc_deficit(self, WS_ilk, WS_eff_ilk, dw_ijlk, hcw_ijlk, dh_ijl, h_il, ct_ilk, D_src_il, **kwargs):
        if not self.deficit_initalized:
            self._calc_layout_terms(dw_ijlk, hcw_ijlk, h_il, dh_ijl, D_src_il, **kwargs)
        return self.mdu_ijlk * (ct_ilk * WS_eff_ilk**2 / WS_ilk)[:, na]

    def wake_radius(self, D_src_il, dw_ijlk, **_):
        # Set at twice the source radius for now
        return np.zeros_like(dw_ijlk) + D_src_il[:, na, :, na]


class LUTInterpolator(object):
    # Faster than scipy.interpolate.interpolate.RegularGridInterpolator
    def __init__(self, x, y, z, V):
        self.x = x
        self.y = y
        self.z = z
        self.V = V
        self.nx = nx = len(x)
        self.ny = ny = len(y)
        self.nz = nz = len(z)
        assert V.shape == (nz, ny, nx)
        self.dx, self.dy = [xy[1] - xy[0] for xy in [x, y]]

        self.x0 = x[0]
        self.y0 = y[0]

        Ve = np.concatenate((V, V[-1:]), 0)
        Ve = np.concatenate((Ve, Ve[:, -1:]), 1)
        Ve = np.concatenate((Ve, Ve[:, :, -1:]), 2)

        self.V000 = np.array([V,
                              Ve[:-1, :-1, 1:],
                              Ve[:-1, 1:, :-1],
                              Ve[:-1, 1:, 1:],
                              Ve[1:, :-1, :-1],
                              Ve[1:, :-1, 1:],
                              Ve[1:, 1:, :-1],
                              Ve[1:, 1:, 1:]]).reshape((8, nz * ny * nx))

    def __call__(self, xyz):
        xp, yp, zp = xyz
        xp = np.maximum(np.minimum(xp, self.x[-1]), self.x[0])
        yp = np.maximum(np.minimum(yp, self.y[-1]), self.y[0])
        # zp = np.maximum(np.minimum(zp, self.z[-1]), self.z[0])

        def i0f(_i):
            _i0 = np.asarray(_i).astype(np.int)
            _if = _i - _i0
            return _i0, _if

        xi0, xif = i0f((xp - self.x0) / self.dx)
        yi0, yif = i0f((yp - self.y0) / self.dy)

        zi0, zif = i0f(np.interp(zp, self.z, np.arange(self.nz)))

        nx, ny = self.nx, self.ny

        v000, v001, v010, v011, v100, v101, v110, v111 = self.V000[:, zi0 * nx * ny + yi0 * nx + xi0]

        v_00 = v000 + (v100 - v000) * zif
        v_01 = v001 + (v101 - v001) * zif
        v_10 = v010 + (v110 - v010) * zif
        v_11 = v011 + (v111 - v011) * zif
        v__0 = v_00 + (v_10 - v_00) * yif
        v__1 = v_01 + (v_11 - v_01) * yif

        return (v__0 + (v__1 - v__0) * xif)
#         # Slightly slower
#         xif1, yif1, zif1 = 1 - xif, 1 - yif, 1 - zif
#         w = np.array([xif1 * yif1 * zif1,
#                       xif * yif1 * zif1,
#                       xif1 * yif * zif1,
#                       xif * yif * zif1,
#                       xif1 * yif1 * zif,
#                       xif * yif1 * zif,
#                       xif1 * yif * zif,
#                       xif * yif * zif])
#
#         return np.sum(w * self.V01[:, zi0, yi0, xi0], 0)


class Fuga(PropagateDownwind):
    def __init__(self, LUT_path, site, windTurbines,
                 rotorAvgModel=RotorCenter(), deflectionModel=None, turbulenceModel=None, remove_wriggles=False):
        """
        Parameters
        ----------
        LUT_path : str
            path to look up tables
        site : Site
            Site object
        windTurbines : WindTurbines
            WindTurbines object representing the wake generating wind turbines
        rotorAvgModel : RotorAvgModel
            Model defining one or more points at the down stream rotors to
            calculate the rotor average wind speeds from.\n
            Defaults to RotorCenter that uses the rotor center wind speed (i.e. one point) only
        deflectionModel : DeflectionModel
            Model describing the deflection of the wake due to yaw misalignment, sheared inflow, etc.
        turbulenceModel : TurbulenceModel
            Model describing the amount of added turbulence in the wake
        """
        PropagateDownwind.__init__(self, site, windTurbines,
                                   wake_deficitModel=FugaDeficit(LUT_path, remove_wriggles=remove_wriggles),
                                   rotorAvgModel=rotorAvgModel, superpositionModel=LinearSum(),
                                   deflectionModel=deflectionModel, turbulenceModel=turbulenceModel)


class FugaBlockage(All2AllIterative):
    def __init__(self, LUT_path, site, windTurbines,
                 rotorAvgModel=RotorCenter(),
                 deflectionModel=None, turbulenceModel=None, convergence_tolerance=1e-6, remove_wriggles=False):
        """
        Parameters
        ----------
        LUT_path : str
            path to look up tables
        site : Site
            Site object
        windTurbines : WindTurbines
            WindTurbines object representing the wake generating wind turbines
        rotorAvgModel : RotorAvgModel
            Model defining one or more points at the down stream rotors to
            calculate the rotor average wind speeds from.\n
            Defaults to RotorCenter that uses the rotor center wind speed (i.e. one point) only
        deflectionModel : DeflectionModel
            Model describing the deflection of the wake due to yaw misalignment, sheared inflow, etc.
        turbulenceModel : TurbulenceModel
            Model describing the amount of added turbulence in the wake
        """
        fuga_deficit = FugaDeficit(LUT_path, remove_wriggles=remove_wriggles)
        All2AllIterative.__init__(self, site, windTurbines, wake_deficitModel=fuga_deficit,
                                  rotorAvgModel=rotorAvgModel, superpositionModel=LinearSum(),
                                  deflectionModel=deflectionModel, blockage_deficitModel=fuga_deficit,
                                  turbulenceModel=turbulenceModel, convergence_tolerance=convergence_tolerance)


def main():
    if __name__ == '__main__':
        from py_wake.examples.data.iea37._iea37 import IEA37Site
        from py_wake.examples.data.iea37._iea37 import IEA37_WindTurbines
        import matplotlib.pyplot as plt

        # setup site, turbines and wind farm model
        site = IEA37Site(16)
        x, y = site.initial_position.T
        windTurbines = IEA37_WindTurbines()

        path = tfp + 'fuga/2MW/Z0=0.03000000Zi=00401Zeta0=0.00E+0/'

        for wf_model in [Fuga(path, site, windTurbines),
                         FugaBlockage(path, site, windTurbines)]:
            plt.figure()
            print(wf_model)

            # run wind farm simulation
            sim_res = wf_model(x, y)

            # calculate AEP
            aep = sim_res.aep().sum()

            # plot wake map
            flow_map = sim_res.flow_map(wd=30, ws=9.8)
            flow_map.plot_wake_map()
            flow_map.plot_windturbines()
            plt.title('AEP: %.2f GWh' % aep)
            plt.show()
        plt.show()


main()
