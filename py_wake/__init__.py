import pkg_resources
from .deficit_models.noj import NOJ, NOJLocal
from .deficit_models.fuga import Fuga, FugaBlockage
from .deficit_models.gaussian import BastankhahGaussian, IEA37SimpleBastankhahGaussian
from .deficit_models.gcl import GCL, GCLLocal
from py_wake.flow_map import HorizontalGrid, XYGrid, YZGrid

plugins = {
    entry_point.name: entry_point.load()
    for entry_point
    in pkg_resources.iter_entry_points('py_wake.plugins')
}

# 'filled_by_setup.py'
__version__ = '2.0.0'
__release__ = '2.0.0'
