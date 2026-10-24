# 多AI模型管理器
from .base import AI
from .registry import register_ai_type, to_ai_type, get_ai_type
import traceback
import json

class MixedAI(AI):
    """
    多AI模型管理器，支持动态切换和配置。
    """
    def __init__(self):
        self.ais = {}
        self.ai = None
        self.current_ai_id = None

    def add(self, id, ai):
        self.ais[id] = ai

    def remove(self, id):
        if id in self.ais.keys():
            a = self.ais[id]
            del self.ais[id]
            if a == self.ai:
                if len(self.ais) == 0:
                    self.ai = None
                else:
                    self.ai = self.ais[list(self.ais.keys())[0]]

    def switch(self, id):
        if id in self.ais.keys():
            self.ai = self.ais[id]
            self.current_ai_id = id
        else:
            raise ValueError(f"No such ai '{id}'")

    def rename(self, id, new_id):
        if id in self.ais.keys():
            self.ais[new_id] = self.ais[id]
            del self.ais[id]

    def generate(self, instruct, console):
        if self.ai:
            return self.ai.generate(instruct, console)
        else:
            def fun():
                yield '', 'no selected ai'
            return fun()

    def save(self, instruct, console, output):
        if self.ai:
            self.ai.save(instruct, console, output)
        else:
            pass

    def print(self, end='\r\n', **kwargs):
        if self.ai:
            self.ai.print(end=end, **kwargs)
        else:
            print("no selected ai", end=end)

    def set(self, key, value):
        if self.ai:
            self.ai.set(key, value)
        else:
            pass

    def get(self, key):
        if self.ai:
            return self.ai.get(key)
        else:
            return None

    def configs(self):
        if self.ai:
            return self.ai.configs()

    @staticmethod
    def from_config(path=None, config=None):
        s = MixedAI()
        try:
            if path:
                with open(path, 'r') as f:
                    config = json.load(f)
            ais = config.get('ai')
            if ais:
                for id in ais.keys():
                    try:
                        c = ais[id]
                        id = c.get('id')
                        t = to_ai_type(c.get('type'))
                        conf = c.get('config')
                        ai = t.from_config(config=conf)
                        s.add(id, ai)
                    except Exception as e:
                        err = traceback.format_exc().replace('\n', '\r\n')
                        print(f"prase ai config '{id}' failed:", err, end='\r\n')
            id = config.get('current_ai_id')
            if id:
                s.switch(id)
        except Exception as e:
            err = traceback.format_exc().replace('\n', '\r\n')
            print(f"prase ai config failed:", err, end='\r\n')
        return s

    def save_config(self, path=None):
        config = {
            'current_ai_id': self.current_ai_id,
            'ai': {},
        }
        for id in self.ais.keys():
            ai = self.ais[id]
            config['ai'][id] = {
                'id': id,
                'type': get_ai_type(ai),
                'config': ai.save_config(),
            }
        if path:
            with open(path, 'w') as f:
                json.dump(config, f)
        return config

register_ai_type('mixed', MixedAI)
