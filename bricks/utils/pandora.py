# -*- coding: utf-8 -*-
# @Time    : 2023-11-14 17:05
# @Author  : Kem
# @Desc    :
import ast
import collections
import importlib
import importlib.metadata as importlib_metadata
import importlib.util
import inspect
import json
import linecache
import os
import re
import subprocess
import sys
from typing import Any, List, Union, Mapping

from loguru import logger

JSONP_REGEX = re.compile(r'\S+?\((?P<obj>[\s\S]*)\)')
PACKAGE_REGEX = re.compile(r"([a-zA-Z0-9_\-]+)([<>=]*)([\d.]*)")


def load_objects(path_or_reference, reload=False):
    """
    Dynamically import modules based on file paths or module names, or import specific properties based on module references

    :param reload:
    :param path_or_reference: file path module name or reference to the module（'module.submodule.attribute'）
    :return: imported modules or properties
    """
    if not isinstance(path_or_reference, str):
        return path_or_reference

    if os.path.sep in path_or_reference or os.path.exists(path_or_reference):
        # 尝试作为文件路径导入
        try:
            module_name = os.path.splitext(os.path.basename(path_or_reference))[0]
            spec = importlib.util.spec_from_file_location(module_name, path_or_reference)

            assert spec and spec.loader, ImportError(f"无法从文件路径导入模块：{path_or_reference}")
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
            return module
        except Exception as e:
            raise e
    else:
        # 尝试作为模块或模块内属性导入
        parts = path_or_reference.split('.')
        for i in range(len(parts), 0, -1):
            module_name = '.'.join(parts[:i])
            try:
                module = importlib.import_module(module_name)
                for attribute in parts[i:]:
                    module = getattr(module, attribute)

                    # 检查模块是否已经导入
                if reload:
                    existing_module = sys.modules.get(module_name)
                    if existing_module:
                        return importlib.reload(existing_module)
                else:
                    return module
            except (ImportError, AttributeError):
                continue
        raise ImportError(f"无法导入指定路径或引用: {path_or_reference}")


def require(package_spec: str) -> str:
    """
    依赖 python 包

    :param package_spec: pymongo==4.6.0 / pymongo
    :return: 安装的包版本号
    """
    # 分离包名和版本规范
    match = PACKAGE_REGEX.match(package_spec)
    assert match, ValueError("无效的包规范")

    package, operator, required_version = match.groups()

    try:
        # 获取已安装的包版本
        installed_version = importlib_metadata.version(package)
        # 检查是否需要安装或更新
        if required_version and not eval(f'{installed_version!r} {operator} {required_version!r}'):
            raise importlib_metadata.PackageNotFoundError
        else:
            return installed_version

    except importlib_metadata.PackageNotFoundError:
        # 包没有安装或版本不符合要求
        install_command = package_spec if required_version else package
        subprocess.check_call([sys.executable, "-m", "pip", "install", install_command])
        logger.debug(f"'{install_command}'已安装/更新。")
        return importlib_metadata.version(package)


def invoke(func, args=None, kwargs: dict = None, annotations: dict = None, namespace: dict = None):
    """
    调用函数, 自动修正参数

    :param func:
    :param args:
    :param kwargs:
    :param annotations:
    :param namespace:
    :return:
    """
    prepared = prepare(func, args, kwargs, annotations, namespace)
    return prepared.func(*prepared.args, **prepared.kwargs)


def prepare(func, args=None, kwargs: dict = None, annotations: dict = None, namespace: dict = None):
    assert callable(func), ValueError(f"func must be callable, but got {type(func)}")
    prepared = collections.namedtuple("prepared", ["func", "args", "kwargs"])

    args = args or []
    kwargs = kwargs or {}
    # 获取已提供参数的名称
    new_args = []
    new_kwargs = {}
    annotations = annotations or {}
    namespace = namespace or {}

    try:
        parameters = inspect.signature(func).parameters
    except:  # noqa
        parameters = {}
        new_args = [*args]
        kwargs and new_args.append(kwargs)

    index = 0

    for name, param in parameters.items():

        if param.kind == inspect.Parameter.VAR_POSITIONAL:
            new_args.extend(args[index:])
            index += len(args[index:])
            continue

        if param.kind == inspect.Parameter.VAR_KEYWORD:
            new_kwargs.update(kwargs)
            continue

        # 参数在 kwargs 里面 -> 从 kwargs 里面取
        # param.default != inspect.Parameter.empty
        if name in kwargs:
            value = kwargs[name]

        # 参数在 namespace 里面 -> 从 namespace 里面取
        elif param.name in namespace:
            value = namespace[param.name]

        # 参数类型存在于 annotations, 并且还可以从 args 里面取值, 并且刚好取到的对应的值也是当前类型 -> 直接从 args 里面取
        elif param.annotation in annotations and index < len(args) and type(args[index]) is param.annotation:
            value = args[index]
            index += 1

        # 参数类型存在于 annotations, -> 从 annotations 里面取
        elif param.annotation in annotations:
            value = annotations[param.annotation]

        # 直接取 args 里面的值
        elif index < len(args):
            value = args[index]
            index += 1

        elif param.default != inspect.Parameter.empty:
            continue

        # 没有传这个参数, 并且也没有可以备选的 annotations  -> 报错
        else:
            raise TypeError(f"missing required argument: {name}, signature: {dict(parameters)}")
        if param.kind in [inspect.Parameter.POSITIONAL_ONLY]:
            new_args.append(value)

        if param.kind in [inspect.Parameter.KEYWORD_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD]:
            new_kwargs[name] = value

    return prepared(func=func, args=new_args, kwargs=new_kwargs)


def iterable(
        _object: Any,
        enforce=(dict, str, bytes, collections.UserDict, Mapping),
        exclude=(),
        convert_null=True
) -> List[Any]:
    """
    用列表将 `exclude` 类型中的其他类型包装起来

    :rtype: object
    :param convert_null: 单个 None 的时候是否给出空列表
    :param exclude: 属于此类型,便不转换
    :param enforce: 属于这里的类型, 便强制转换, 不检查 iter, 优先级第一
    :param _object:
    :return:
    """
    if _object is None and convert_null:
        return []

    if isinstance(_object, enforce) and not isinstance(_object, exclude):
        return [_object]

    elif isinstance(_object, exclude) or hasattr(_object, "__iter__"):
        return _object

    else:
        return [_object]


def single(_object, default=None):
    """
    将元素变为可迭代对象后, 获取其第一个元素

    :param _object:
    :param default:
    :return:
    """
    return next((i for i in iterable(_object)), default)


def json_or_eval(text, jsonp=False, errors="strict", _step=0, **kwargs) -> Union[dict, list, str]:
    """
    通过字符串获取 python 对象，支持json类字符串和jsonp字符串

    :param _step:
    :param jsonp: 是否为 jsonp
    :param errors: 错误处理方法
    :param text: 需要解序列化的文本
    :return:

    """

    def literal_eval():
        return ast.literal_eval(text)

    def json_decode():
        return json.loads(text)

    def use_jsonp():
        real_text = JSONP_REGEX.search(text).group('obj')
        return json_or_eval(real_text, jsonp=True, _step=_step + 1, **kwargs)

    if not isinstance(text, str):
        return text

    funcs = [json_decode, literal_eval]
    jsonp and _step == 0 and funcs.append(use_jsonp)
    for func in funcs:
        try:
            return func()
        except:  # noqa
            pass

    else:
        assert errors == "ignore", ValueError(f'illegal json string: `{text}`')
        return text


def get_simple_stack(e):
    # 获取当前时间

    # 获取异常的堆栈跟踪
    tb = e.__traceback__

    # 开始格式化
    formatted_trace = f""

    while tb is not None:
        # 获取当前堆栈帧的详细信息
        frame = tb.tb_frame
        lineno = tb.tb_lineno
        code = frame.f_code
        filename = code.co_filename

        # 获取出错的代码行
        line = linecache.getline(filename, lineno).strip()

        formatted_trace += f"  File \"{filename}\", line {lineno}, in {code.co_name}\n"
        formatted_trace += f"    {line}\n"
        tb = tb.tb_next
    return formatted_trace


def clean_rows(*rows: dict, **layout):
    """
    清洗数据

    :param rows: 需要处理的数据
    :param layout:
        - rename: 修改名字
        - show: 移除 / 保留 / 函数
        - factory: 工厂函数, 动态处理
        - default: 默认值
    :return:
    """

    def _rename(rule: dict, data: dict):
        for oldname, newname in rule.items():
            data[newname] = data.pop(oldname, None)

    def _show(rule: dict, data: dict):
        for key, flag in rule.items():
            if (
                    callable(flag) and not invoke(flag, args=[data.get(key, None)], kwargs={"row": row})
                    or not flag
            ):
                rule.pop(key, None)

    def _factory(rule: dict, data: dict):
        for key, func in rule.items():
            if isinstance(func, str):
                func = load_objects(func)
            data[key] = invoke(func, args=[data.get(key, None)], kwargs={"row": row})

    def _default(rule: dict, data: dict):
        for key, default in rule.items():
            data.setdefault(key, default)

    flows = [
        (layout.get("default") or {}, _default),
        (layout.get("show") or {}, _show),
        (layout.get("factory") or {}, _factory),
        (layout.get("rename") or {}, _rename),

    ]
    for row in rows:
        for _rule, flow in flows:
            _rule and flow(_rule, row)

    else:
        return list(rows)


if __name__ == '__main__':
    # print(require("pandas"))
    my_data = [{"id": i, "name": i} for i in range(100)]
    print(clean_rows(
        *my_data,
        default={"hobby": "nothing"},
        rename={"id": "uid"},
        factory={"name": lambda x: "name: " + str(x)}
    ))
    print(my_data)
