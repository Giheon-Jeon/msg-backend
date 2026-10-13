DEFAULT_PAGE = 1
DEFAULT_SIZE = 20
MAX_SIZE = 100
MAX_PAGE = 10_000


def parse_pagination(page_param, size_param):
    try:
        page = int(page_param)
    except (TypeError, ValueError):
        page = DEFAULT_PAGE

    if page < 1:
        page = DEFAULT_PAGE

    if page > MAX_PAGE:
        page = MAX_PAGE

    try:
        size = int(size_param)
    except (TypeError, ValueError):
        size = DEFAULT_SIZE

    if size < 1:
        size = DEFAULT_SIZE

    if size > MAX_SIZE:
        size = MAX_SIZE

    return page, size
