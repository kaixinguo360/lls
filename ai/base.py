# AI 基类

class AI:
    """
    AI基类，定义通用接口。
    """
    def generate(self, instruct, console):
        pass

    def save(self, instruct, console, output):
        pass

    def print(self, **kwargs):
        pass

    def set(self, key, value):
        old = self.get(key)
        if old is not None:
            if isinstance(old, int):
                value = int(value)
            elif isinstance(old, float):
                value = float(value)
            elif isinstance(old, str):
                value = str(value)
            else:
                raise ValueError(f"unsupport argument type '{type(old).__name__}'")
        setattr(self, key, value)

    def get(self, key):
        return getattr(self, key)

    def configs(self):
        c = [i for i in self.__dict__.items() if i[0][:1] != '_']
        return sorted(c, key=lambda x:x[0])

    def printConfigs(self, end='\r\n'):
        for c in self.configs():
            key = c[0]
            value = str(c[1])
            _type = type(c[1]).__name__
            if isinstance(value, str):
                value = value.replace('\n', '\\n')
                if len(value) > 30:
                    value = value[:30] + '...'
            print(f"({_type}) {key} = {value}", end=end)

    @staticmethod
    def from_config(**kwargs):
        raise NotImplementedError

    def save_config(self, **kwargs):
        raise NotImplementedError
