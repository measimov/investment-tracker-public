"""读模型：由输入模型派生、去掉输入约束的响应字段（#283）。

响应 schema 此前直接继承输入模型（`TransactionResponse(TransactionBase)`），把
`gt=0`、`max_length`、`pattern` 与输入侧 validator（空白拒绝、市场白名单、代码归一）
一并带到了**序列化**上：导入器、脚本或迁移只要写入一行不满足的数据（零成本转入、
历史脏数据），整个列表端点 500。约束属于写入口，读端点必须能如实展示库里的任何行。

`read_fields(Base)` 逐字段复制类型、默认值与说明，丢掉约束元数据；validator 定义在
类上，不随字段复制。响应模型写成 `class XxxResponse(read_model(XxxBase))`，输入模型
新增字段会自动出现在响应里，两者不会漂移。`tests/test_read_models.py` 守护所有
response_model（含嵌套）都不带输入约束。
"""

from typing import Any, Dict, Tuple, Type

from pydantic import BaseModel, Field, create_model


def read_fields(base: Type[BaseModel], **overrides: Tuple[Any, Any]) -> Dict[str, Tuple[Any, Any]]:
    """base 的字段 → (类型, Field(默认值, 说明))，不含 gt/ge/max_length/pattern 等约束。

    overrides 按字段名替换（嵌套输入模型要换成对应的读模型，EmailStr 之类的校验型
    类型要换成 str）。
    """
    fields: Dict[str, Tuple[Any, Any]] = {}
    for name, info in base.model_fields.items():
        if info.default_factory is not None:
            spec = Field(default_factory=info.default_factory, description=info.description)
        elif info.is_required():
            spec = Field(..., description=info.description)
        else:
            spec = Field(info.default, description=info.description)
        fields[name] = (info.annotation, spec)
    fields.update(overrides)
    return fields


def read_model(base: Type[BaseModel], **overrides: Tuple[Any, Any]) -> Type[BaseModel]:
    """用作响应模型的基类：`class TransactionResponse(read_model(TransactionBase)): ...`。"""
    return create_model(f"{base.__name__}ReadFields", **read_fields(base, **overrides))
