import copy
import inspect
import warnings
from collections import namedtuple

import marshmallow as m
from marshmallow.validate import Range
from marshmallow.validate import OneOf
from marshmallow.validate import Length
from marshmallow.validate import Validator

from plangrid.flask_toolbox.validation import QueryParamList
from plangrid.flask_toolbox.validation import CommaSeparatedList
from plangrid.flask_toolbox.validation import DisallowExtraFieldsMixin
from plangrid.flask_toolbox.framing import swagger_words as sw


marshmallow_version = tuple(int(v) for v in m.__version__.split('.'))

if not (2, 13, 5) <= marshmallow_version < (3, 0, 0):
    warnings.warn(
        'Version {} of Marshmallow is not supported yet! '
        'Proceed with caution.'.format(m.__version__)
    )


# Special value to signify that a JSONSchema field should be left unset
class UNSET(object):
    pass


# Marshmallow schemas work differently in different directions (i.e. "dump" vs
# "load"), and we have to convert them to swagger accordingly.
# These are special values that we'll use to signify the direction to
# converters.
class IN(object):
    pass


class OUT(object):
    pass


# We'll use this to mark methods as JSONSchema attribute setters
_method_marker = '__sets_jsonschema_attr__'

# Holds attributes that we can pass around in these recursive
# calls to converters. Bit messy, but :shrug:
_Context = namedtuple('_Context', [
    # This will hold a reference to a convert method that can be used
    # to make recursive calls
    'convert',

    # This will be either IN or OUT, and signifies if the converter should
    # treat the marshmallow schema as "load"ing or "dump"ing.
    'direction',

    # Only really using this for validators at the moment. It will hold the
    # JSONSchema object that's been converter so far, so that the validator
    # can be converted based on the type of the schema.
    'memo',

    # The current schema being converted.
    'schema'
])


def _normalize_validate(validate):
    """
    Coerces the validate attribute on a Marshmallow field to a consistent type.

    The validate attribute on a Marshmallow field can either be a single
    Validator or a collection of Validators.

    :param Validator|list[Validator] validate:
    :rtype: list[Validator]
    """
    if callable(validate):
        return [validate]
    else:
        return validate


def get_schema_title(schema):
    """
    Gets a title for the given Marshmallow schema. This title will be used
    as a name/key for the object in swagger.

    :param m.Schema schema:
    :rtype: str
    """
    if hasattr(schema, '__swagger_title__'):
        return schema.__swagger_title__
    elif hasattr(schema, '__name__'):
        return schema.__name__
    else:
        return schema.__class__.__name__


def sets_jsonschema_attr(attr):
    """
    Decorates a `MarshmallowConverter` method, marking it as an JSONSchema
    attribute setter.

    Example usage::

        class Converter(MarshmallowConverter):

        MARSHMALLOW_TYPE = String()

        @sets_jsonschema_attr('type')
        def get_type(obj, context):
            return 'string'

    This converter receives instances of `String` and converts it to a
    JSONSchema object that looks like `{'type': 'string'}`.

    :param str attr: The attribute to set
    """
    def wrapper(f):
        setattr(f, _method_marker, attr)
        return f
    return wrapper


class MarshmallowConverter(object):
    """
    Abstract class for objects that convert Marshmallow objects to
    JSONSchema dictionaries.
    """

    MARSHMALLOW_TYPE = None

    def convert(self, obj, context):
        """
        Converts a Marshmallow object to a JSONSchema dictionary.

        This inspects the converter instance for methods that have been
        marked as attribute setters, calling them to set attributes on the
        resulting JSONSchema dictionary.

        :param m.Schema|m.fields.Field|Validator obj:
            The Marshmallow object to be converted
        :param _Context context:
            Various information to help the converter understand how to
            convert the object.
        :rtype: dict
        """
        jsonschema_obj = {}

        for _, method in inspect.getmembers(self, predicate=inspect.ismethod):
            if hasattr(method, _method_marker):
                val = method(obj, context)
                if val is not UNSET:
                    jsonschema_obj[getattr(method, _method_marker)] = val

        return jsonschema_obj


class SchemaConverter(MarshmallowConverter):
    """Converts Marshmallow Schema objects."""

    MARSHMALLOW_TYPE = m.Schema

    @sets_jsonschema_attr(sw.type_)
    def get_type(self, obj, context):
        if obj.many:
            return sw.array
        else:
            return sw.object_

    @sets_jsonschema_attr(sw.items)
    def get_items(self, obj, context):
        if not obj.many:
            return UNSET

        singular_obj = copy.deepcopy(obj)
        singular_obj.many = False

        return context.convert(singular_obj, context)

    @sets_jsonschema_attr(sw.properties)
    def get_properties(self, obj, context):
        if obj.many:
            return UNSET

        properties = {}

        for name, field in obj.fields.items():
            prop = name
            if context.direction == OUT and field.dump_to:
                prop = field.dump_to
            elif context.direction == IN and field.load_from:
                prop = field.load_from
            properties[prop] = context.convert(field, context)

        return properties

    @sets_jsonschema_attr(sw.required)
    def get_required(self, obj, context):
        if obj.many:
            return UNSET

        required = []

        for name, field in obj.fields.items():
            if field.required:
                prop = name
                if context.direction == OUT and field.dump_to:
                    prop = field.dump_to
                elif context.direction == IN and field.load_from:
                    prop = field.load_from
                required.append(prop)

        return required if required else UNSET

    @sets_jsonschema_attr(sw.description)
    def get_description(self, obj, context):
        if obj.many:
            return UNSET
        elif obj.__doc__:
            return obj.__doc__
        else:
            return UNSET

    @sets_jsonschema_attr(sw.title)
    def get_title(self, obj, context):
        if not obj.many:
            return get_schema_title(obj)
        else:
            return UNSET


class FieldConverter(MarshmallowConverter):
    """
    Base Converter for Marshmallow Field objects.

    This should be extended for specific Field types.
    """

    MARSHMALLOW_TYPE = m.fields.Field

    def convert(self, obj, context):
        jsonschema_obj = super(FieldConverter, self).convert(obj, context)

        if obj.validate:
            validators = _normalize_validate(obj.validate)

            for validator in validators:
                jsonschema_obj.update(
                    context.convert(
                        obj=validator,
                        context=_Context(
                            convert=context.convert,
                            direction=context.direction,
                            memo=jsonschema_obj,
                            schema=context.schema
                        )
                    )
                )

        return jsonschema_obj

    @sets_jsonschema_attr(sw.default)
    def get_default(self, obj, context):
        if obj.missing is not m.missing:
            return obj.missing
        else:
            return UNSET

    @sets_jsonschema_attr(sw.nullable)
    def get_nullable(self, obj, context):
        if obj.allow_none is not False:
            return True
        else:
            return UNSET

    @sets_jsonschema_attr(sw.description)
    def get_description(self, obj, context):
        if 'description' in obj.metadata:
            return obj.metadata['description']
        else:
            return UNSET


class ValidatorConverter(MarshmallowConverter):
    """
    Base Converter for Marshmallow Validator objects.

    This should be extended for specific Validator types.
    """

    MARSHMALLOW_TYPE = Validator


class DisallowExtraFieldsConverter(SchemaConverter):
    MARSHMALLOW_TYPE = DisallowExtraFieldsMixin

    @sets_jsonschema_attr(sw.additional_properties)
    def get_additional_properties(self, obj, context):
        return False


class NestedConverter(FieldConverter):
    MARSHMALLOW_TYPE = m.fields.Nested

    def convert(self, obj, context):
        nested_obj = obj.nested

        # instantiate the object because the converter expects it to be
        inst = nested_obj()

        if obj.many:
            return {
                sw.type_: sw.array,
                sw.items: context.convert(inst, context)
            }
        else:
            return context.convert(inst, context)


class ListConverter(FieldConverter):
    MARSHMALLOW_TYPE = m.fields.List

    @sets_jsonschema_attr(sw.type_)
    def get_type(self, obj, context):
        return sw.array

    @sets_jsonschema_attr(sw.items)
    def get_items(self, obj, context):
        return context.convert(obj.container, context)


class DictConverter(FieldConverter):
    MARSHMALLOW_TYPE = m.fields.Dict

    @sets_jsonschema_attr(sw.type_)
    def get_type(self, obj, context):
        return sw.object_


class IntegerConverter(FieldConverter):
    MARSHMALLOW_TYPE = m.fields.Integer

    @sets_jsonschema_attr(sw.type_)
    def get_type(self, obj, context):
        return sw.integer


class StringConverter(FieldConverter):
    MARSHMALLOW_TYPE = m.fields.String

    @sets_jsonschema_attr(sw.type_)
    def get_type(self, obj, context):
        return sw.string


class NumberConverter(FieldConverter):
    MARSHMALLOW_TYPE = m.fields.Number

    @sets_jsonschema_attr(sw.type_)
    def get_type(self, obj, context):
        return sw.number


class BooleanConverter(FieldConverter):
    MARSHMALLOW_TYPE = m.fields.Boolean

    @sets_jsonschema_attr(sw.type_)
    def get_type(self, obj, context):
        return sw.boolean


class DateTimeConverter(FieldConverter):
    MARSHMALLOW_TYPE = m.fields.DateTime

    @sets_jsonschema_attr(sw.type_)
    def get_type(self, obj, context):
        return sw.string

    @sets_jsonschema_attr(sw.format_)
    def get_format(self, obj, context):
        return sw.date_time


class UUIDConverter(FieldConverter):
    MARSHMALLOW_TYPE = m.fields.UUID

    @sets_jsonschema_attr(sw.type_)
    def get_type(self, obj, context):
        return sw.string

    @sets_jsonschema_attr(sw.format_)
    def get_format(self, obj, context):
        return sw.uuid


class DateConverter(FieldConverter):
    MARSHMALLOW_TYPE = m.fields.Date

    @sets_jsonschema_attr(sw.type_)
    def get_type(self, obj, context):
        return sw.string

    @sets_jsonschema_attr(sw.format_)
    def get_format(self, obj, context):
        return sw.date


class MethodConverter(FieldConverter):
    MARSHMALLOW_TYPE = m.fields.Method

    @sets_jsonschema_attr(sw.type_)
    def get_type(self, obj, context):
        err_msg = (
            '__rtype__ attribute is required '
            'for swagger generation to work'
        )

        if context.direction is IN:
            meth = getattr(context.schema, obj.deserialize_method_name)

            if not hasattr(meth, '__rtype__'):
                raise ValueError(err_msg)

            return meth.__rtype__
        else:
            meth = getattr(context.schema, obj.serialize_method_name)

            if not hasattr(meth, '__rtype__'):
                raise ValueError(err_msg)

            return meth.__rtype__


class FunctionConverter(FieldConverter):
    MARSHMALLOW_TYPE = m.fields.Function

    @sets_jsonschema_attr(sw.type_)
    def get_type(self, obj, context):
        err_msg = (
            '__rtype__ attribute is required '
            'for swagger generation to work'
        )

        if context.direction is IN:
            if not hasattr(obj.deserialize_func, '__rtype__'):
                raise ValueError(err_msg)

            return obj.deserialize_func.__rtype__
        else:
            if not hasattr(obj.serialize_func, '__rtype__'):
                raise ValueError(err_msg)

            return obj.serialize_func.__rtype__


class CsvArrayConverter(ListConverter):
    MARSHMALLOW_TYPE = CommaSeparatedList

    @sets_jsonschema_attr(sw.collection_format)
    def get_collection_format(self, obj, context):
        return sw.csv


class MultiArrayConverter(ListConverter):
    MARSHMALLOW_TYPE = QueryParamList

    @sets_jsonschema_attr(sw.collection_format)
    def get_collection_format(self, obj, context):
        return sw.multi


class RangeConverter(ValidatorConverter):
    MARSHMALLOW_TYPE = Range

    @sets_jsonschema_attr(sw.minimum)
    def get_minimum(self, obj, context):
        if obj.min is not None:
            return obj.min
        else:
            return UNSET

    @sets_jsonschema_attr(sw.maximum)
    def get_maximum(self, obj, context):
        if obj.max is not None:
            return obj.max
        else:
            return UNSET


class OneOfConverter(ValidatorConverter):
    MARSHMALLOW_TYPE = OneOf

    @sets_jsonschema_attr(sw.enum)
    def get_enum(self, obj, context):
        return list(obj.choices)


class LengthConverter(ValidatorConverter):
    MARSHMALLOW_TYPE = Length

    @sets_jsonschema_attr(sw.min_items)
    def get_minimum_items(self, obj, context):
        if context.memo[sw.type_] == sw.array:
            if obj.min is not None:
                return obj.min
        return UNSET

    @sets_jsonschema_attr(sw.max_items)
    def get_maximum_items(self, obj, context):
        if context.memo[sw.type_] == sw.array:
            if obj.max is not None:
                return obj.max
        return UNSET

    @sets_jsonschema_attr(sw.min_length)
    def get_minimum_length(self, obj, context):
        if context.memo[sw.type_] == sw.string:
            if obj.min is not None:
                return obj.min
        return UNSET

    @sets_jsonschema_attr(sw.max_length)
    def get_maximum_length(self, obj, context):
        if context.memo[sw.type_] == sw.string:
            if obj.max is not None:
                return obj.max
        return UNSET


class ConverterRegistry(object):
    """
    Registry for `MarshmallowConverter`s.

    Schemas for responses, query strings, request bodies, etc. need to
    be converted differently. For example, response schemas as "dump"ed and
    request body schemas are "loaded". For another example, query strings
    don't support nesting.

    This registry also allows for additional converters to be added for custom
    Marshmallow types.

    :param direction:
        OUT if this registry is used to convert output schemas (e.g. response
        schemas) and IN if tis registry is used to convert input schemas
        (e.g. request body schemas).
    """
    def __init__(self, direction=OUT):
        self._type_map = {}
        self._validator_map = {}
        self.direction = direction

    def register_type(self, converter):
        """
        Registers a converter.

        :param MarshmallowConverter converter:
        """
        self._type_map[converter.MARSHMALLOW_TYPE] = converter

    def register_types(self, converters):
        """
        Registers multiple converters.

        :param iterable[MarshmallowConverter] converters:
        """
        for converter in converters:
            self.register_type(converter)

    def _convert(self, obj, context):
        """
        Converts a Marshmallow object to a JSONSchema dictionary.

        :param m.Schema|m.fields.Field|Validator obj:
            The Marshmallow object to be converted
        :param _Context context:
            Various information to help the converter understand how to
            convert the given object.

            This helps with all the recursive nonsense.
        :rtype: dict
        """
        method_resolution_order = obj.__class__.__mro__

        for cls in method_resolution_order:
            if cls in self._type_map:
                return self._type_map[cls].convert(obj=obj, context=context)
        else:
            raise ValueError(
                'No registered type found in method resolution order: {mro}\n'
                'Registered types: {types}'.format(
                    mro=method_resolution_order,
                    types=list(self._type_map.keys())
                )
            )

    def convert(self, obj):
        """
        Converts a Marshmallow object to a JSONSchema dictionary.

        :param m.Schema|m.fields.Field|Validator obj:
            The Marshmallow object to be converted
        :rtype: dict
        """
        return self._convert(
            obj=obj,
            context=_Context(
                convert=self._convert,
                direction=self.direction,
                memo={},
                schema=obj
            )
        )


def convert_jsonschema_to_list_of_parameters(obj, in_='query'):
    """
    Swagger is only _based_ on JSONSchema. Query string and header parameters
    are represented as list, not as an object. This converts a JSONSchema
    object (as return by the converters) to a list of parameters suitable for
    swagger.

    :param dict obj:
    :param str in_: 'query' or 'header'
    :rtype: list[dict]
    """
    parameters = []

    assert obj['type'] == 'object'

    required = obj.get('required', [])

    for name, prop in obj['properties'].items():
        parameter = copy.deepcopy(prop)
        parameter['required'] = name in required
        parameter['in'] = in_
        parameter['name'] = name
        parameters.append(parameter)

    return parameters


COMMON_CONVERTERS = (
    SchemaConverter(),
    IntegerConverter(),
    NumberConverter(),
    StringConverter(),
    DateConverter(),
    DateTimeConverter(),
    BooleanConverter(),
    UUIDConverter(),
    ListConverter(),
    RangeConverter(),
    LengthConverter(),
    OneOfConverter(),
    MethodConverter(),
    FunctionConverter()
)

QUERY_STRING_CONVERTERS = COMMON_CONVERTERS + (
    CsvArrayConverter(),
    MultiArrayConverter()
)

REQUEST_BODY_CONVERTERS = COMMON_CONVERTERS + (
    NestedConverter(),
    DictConverter(),
    DisallowExtraFieldsConverter()
)

HEADER_CONVERTERS = QUERY_STRING_CONVERTERS

RESPONSE_CONVERTERS = REQUEST_BODY_CONVERTERS

ALL_CONVERTERS = COMMON_CONVERTERS + (
    CsvArrayConverter(),
    MultiArrayConverter(),
    NestedConverter(),
    DictConverter(),
    DisallowExtraFieldsConverter()
)

query_string_converter_registry = ConverterRegistry(direction=IN)
query_string_converter_registry.register_types(QUERY_STRING_CONVERTERS)

request_body_converter_registry = ConverterRegistry(direction=IN)
request_body_converter_registry.register_types(REQUEST_BODY_CONVERTERS)

headers_converter_registry = ConverterRegistry(direction=IN)
headers_converter_registry.register_types(HEADER_CONVERTERS)

response_converter_registry = ConverterRegistry(direction=OUT)
response_converter_registry.register_types(RESPONSE_CONVERTERS)