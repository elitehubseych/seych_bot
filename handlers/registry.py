COMMANDS = {}

DEAD_SESSION = object()


def command(*names):

    def wrapper(func):
        for name in names:
            key = name.strip().lower()
            if key in COMMANDS:
                raise ValueError(f"Команда '{key}' уже зарегистрирована")
            COMMANDS[key] = func
        return func

    return wrapper
