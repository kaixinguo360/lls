# AI 类型注册与发现机制

_ai_types = {}

def register_ai_type(id, t):
    """注册AI类型"""
    global _ai_types
    _ai_types[id] = t

def to_ai_type(id):
    """根据id获取AI类型"""
    global _ai_types
    return _ai_types[id]

def get_ai_type(ai):
    """根据实例获取AI类型id"""
    global _ai_types
    for id in _ai_types:
        t = _ai_types[id]
        if type(ai) == t:
            return id
    return 'unknown'
