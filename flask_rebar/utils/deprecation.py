"""
    General-Purpose Utilities
    ~~~~~~~~~~~~~~~~~~~~~~~~~

    Utilities that are not specific to request-handling (you'll find those in request_utils.py).

    :copyright: Copyright 2019 Autodesk, Inc., see AUTHORS.
    :license: MIT, see LICENSE for details.
"""
from __future__ import annotations
import functools
import warnings
from typing import Any, Callable, Dict, NamedTuple, Optional, Tuple, TypeVar, Union
from typing_extensions import ParamSpec

from werkzeug.local import LocalProxy as module_property  # noqa


# ref http://jtushman.github.io/blog/2014/05/02/module-properties/ for background on
# use of werkzeug LocalProxy to simulate "module properties"
# end result: singleton config can be accessed by, e.g.,
#    from flask_rebar.utils.deprecation import config as deprecation_config
#    deprecation_config.warning_type = YourFavoriteWarning


T = TypeVar("T")
P = ParamSpec("P")


class DeprecationConfig:
    """
    Singleton class to allow one-time set of deprecation config controls
    """

    __instance = None

    @staticmethod
    def getInstance() -> DeprecationConfig:
        """Static access method."""
        if DeprecationConfig.__instance is None:
            return DeprecationConfig()
        return DeprecationConfig.__instance

    def __init__(self) -> None:
        """Virtually private constructor."""
        if DeprecationConfig.__instance is not None:
            raise Exception("This class is a singleton!")
        else:
            DeprecationConfig.__instance = self
            self.warning_type = FutureWarning


@module_property
def config() -> DeprecationConfig:
    return DeprecationConfig.getInstance()


def deprecated(
    new_func: Optional[Union[str, Tuple[str, str]]] = None,
    eol_version: Optional[str] = None,
) -> Callable:
    """
    :param Union[str, (str, str)] new_func: Name (or name and end-of-life version) of replacement
    :param str eol_version: Version in which this function may no longer work
    :return:
    Raise a deprecation warning for decorated function.
    Tuple is supported for new_func just in case somebody infers it as an option based on the way we
    deprecate params..
    If tuple form is used for new_func AND eol_version is provided, eol_version will trump whatever is
    found in the tuple; caveat emptor
    """

    def decorator(f: Callable[P, T]) -> Callable[P, T]:
        @functools.wraps(f)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            new, eol, _ = _validated_deprecation_spec(new_func)
            eol = eol_version or eol
            _deprecation_warning(f.__name__, new, eol, stacklevel=3)
            return f(*args, **kwargs)

        return wrapper

    return decorator


def deprecated_parameters(**aliases: Any) -> Callable:
    """
    Adapted from https://stackoverflow.com/a/49802489/977046
    :param aliases: Keyword args in the form {old_param_name = Union[new_param_name, (new_param_name, eol_version),
                    (new_param_name, eol_version, coerce_func)]}
                    where eol_version is the version in which the alias may case to be recognized and coerce_func is a
                    function used to coerce old values to new values.
    :return: function decorator that will apply aliases to param names and raise DeprecationWarning
    """

    def decorator(f: Callable[P, T]) -> Callable[P, T]:
        @functools.wraps(f)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            new_kwargs = _remap_kwargs(f.__name__, kwargs, aliases)
            return f(*args, **new_kwargs)

        return wrapper

    return decorator


class DeprecationSpec(NamedTuple):
    new_name: Optional[str]
    eol_version: Optional[str]
    coerce_func: Optional[Callable]


def _validated_deprecation_spec(
    spec: Optional[Union[str, Tuple[Any, ...]]]
) -> DeprecationSpec:
    """
    :param Union[new_name, (new_name, eol_version), (new_name, eol_version, coerce_func)] spec:
        new name and/or expected end-of-life version
    :return: (str new_name, str eol_version, func coerce_func),
        normalized to tuple and sanitized to deal with malformed inputs
    Parse a deprecation spec (string or tuple) to a standardized namedtuple form.
    If spec is provided as a bare value (presumably string), we'll treat as new name with no end-of-life version
    If spec is provided (likely on accident) as a 1-element tuple, we'll treat same as a bare value
    If spec is provided as a tuple with more than 3 elements, we'll simply ignore the extraneous
    """
    new_name = None
    eol_version = None
    coerce_func = None
    if type(spec) is tuple:
        if len(spec) > 0:
            new_name = str(spec[0]) if spec[0] else None
        if len(spec) > 1:
            eol_version = str(spec[1]) if spec[1] else None
        if len(spec) > 2:
            coerce_func = spec[2]
    elif spec:
        new_name = str(spec)
    validated = DeprecationSpec(new_name, eol_version, coerce_func)
    return validated


def _remap_kwargs(
    func_name: str, kwargs: Dict[str, Any], aliases: Dict[str, str]
) -> Dict[str, Any]:
    """
    Adapted from https://stackoverflow.com/a/49802489/977046
    """
    remapped_args = dict(kwargs)
    for alias, new_spec in aliases.items():
        if alias in remapped_args:
            new, eol_version, coerce_func = _validated_deprecation_spec(new_spec)
            if new in remapped_args:
                raise TypeError(f"{func_name} received both {alias} and {new}")
            else:
                _deprecation_warning(alias, new, eol_version, stacklevel=4)
                if new:
                    value = remapped_args.pop(alias)
                    if coerce_func is not None:
                        value = coerce_func(value)
                    remapped_args[new] = value
    return remapped_args


def _deprecation_warning(
    old_name: str,
    new_name: Optional[str],
    eol_version: Optional[str],
    stacklevel: int = 1,
) -> None:
    eol_clause = f" and may be removed in version {eol_version}" if eol_version else ""
    replacement_clause = f"; use {new_name}" if new_name else ""
    msg = f"{old_name} is deprecated{eol_clause}{replacement_clause}"
    warnings.warn(message=msg, category=config.warning_type, stacklevel=stacklevel)  # type: ignore
