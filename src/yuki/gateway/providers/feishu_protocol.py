"""Feishu's public pbbp2 Frame schema, through the standard protobuf runtime.

Wire fields from larksuite/oapi-sdk-python, commit
0d9749a4728446a0ffa2e8374b37b5e1ce1cfd9c/lark_oapi/ws/pb/pbbp2_pb2.py.
The SDK's Go-only gogo annotation has no wire meaning and isn't imported.

MIT License
Copyright (c) 2023 Lark Technologies Pte. Ltd.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

from typing import Any

from google.protobuf import descriptor_pb2, descriptor_pool, message_factory

_schema = descriptor_pb2.FileDescriptorProto(
    name="yuki_feishu_frame.proto", package="pbbp2", syntax="proto2"
)
_header = _schema.message_type.add(name="Header")
for _number, _name in ((1, "key"), (2, "value")):
    _header.field.add(name=_name, number=_number, type=9, label=2)
_frame = _schema.message_type.add(name="Frame")
for _number, _name, _type, _label in (
    (1, "SeqID", 4, 2),
    (2, "LogID", 4, 2),
    (3, "service", 5, 2),
    (4, "method", 5, 2),
    (5, "headers", 11, 3),
    (6, "payload_encoding", 9, 1),
    (7, "payload_type", 9, 1),
    (8, "payload", 12, 1),
    (9, "LogIDNew", 9, 1),
):
    _field = _frame.field.add(name=_name, number=_number, type=_type, label=_label)
    if _name == "headers":
        _field.type_name = ".pbbp2.Header"
_descriptor = descriptor_pool.DescriptorPool().AddSerializedFile(_schema.SerializeToString())
Frame: Any = message_factory.GetMessageClass(_descriptor.message_types_by_name["Frame"])
