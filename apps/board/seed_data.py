from apps.board.models import Cell, ChanceCard


BOARD_SIZE = 36
FIRST_CELL_INDEX = 1

SPECIAL_CELLS = {
    1: (Cell.CellType.START, "출발"),
    7: (Cell.CellType.CHANCE, "찬스"),
    16: (Cell.CellType.ROULETTE, "룰렛"),
    21: (Cell.CellType.AIRPORT, "세계여행"),
    25: (Cell.CellType.ROULETTE, "룰렛"),
    30: (Cell.CellType.CHANCE, "황금열쇠"),
}

DIFFICULTY_COUNTS = {
    Cell.Difficulty.HARD: 6,
    Cell.Difficulty.MEDIUM: 12,
    Cell.Difficulty.EASY: 12,
}

# 강지원 확정 (2026-08-19): 칸별 난이도. cell_index -> Cell.Difficulty
CELL_DIFFICULTY = {
    2: Cell.Difficulty.HARD,
    3: Cell.Difficulty.MEDIUM,
    4: Cell.Difficulty.HARD,
    5: Cell.Difficulty.EASY,
    6: Cell.Difficulty.MEDIUM,
    8: Cell.Difficulty.MEDIUM,
    9: Cell.Difficulty.EASY,
    10: Cell.Difficulty.HARD,
    11: Cell.Difficulty.EASY,
    12: Cell.Difficulty.MEDIUM,
    13: Cell.Difficulty.MEDIUM,
    14: Cell.Difficulty.EASY,
    15: Cell.Difficulty.MEDIUM,
    17: Cell.Difficulty.EASY,
    18: Cell.Difficulty.MEDIUM,
    19: Cell.Difficulty.EASY,
    20: Cell.Difficulty.MEDIUM,
    22: Cell.Difficulty.MEDIUM,
    23: Cell.Difficulty.EASY,
    24: Cell.Difficulty.MEDIUM,
    26: Cell.Difficulty.HARD,
    27: Cell.Difficulty.EASY,
    28: Cell.Difficulty.EASY,
    29: Cell.Difficulty.HARD,
    31: Cell.Difficulty.MEDIUM,
    32: Cell.Difficulty.MEDIUM,
    33: Cell.Difficulty.EASY,
    34: Cell.Difficulty.EASY,
    35: Cell.Difficulty.HARD,
    36: Cell.Difficulty.EASY,
}

DIFFICULTY_LABELS = {
    Cell.Difficulty.HARD: "상",
    Cell.Difficulty.MEDIUM: "중",
    Cell.Difficulty.EASY: "하",
}

# 강지원 확정 (2026-08-19): 동아리 배정표 기준 분야 x 난이도별 출제 동아리.
# 슬래시(/)로 두 동아리가 적힌 칸은 문제 2개(동아리별 1개)를 뜻한다.
# 동아리명 철자는 강지원 확정: SeKurity, CodeCure
CATEGORY_CLUB_ASSIGNMENTS = {
    Cell.Difficulty.HARD: {
        "PWN": ["SWING"],
        "WEB": ["MJSEC"],
        "REV": ["Y-CERT"],
        "CRYPTO": ["SeKurity"],
        "WEB3": ["CodeCure"],
        "MISC": ["Aegis"],
    },
    Cell.Difficulty.MEDIUM: {
        "PWN": ["SeKurity", "Aegis"],
        "WEB": ["Aegis", "Y-CERT"],
        "REV": ["MJSEC"],
        "FORENSIC": ["CodeCure", "SWING"],
        "CRYPTO": ["SWING", "Y-CERT"],
        "MISC": ["SeKurity", "MJSEC"],
        "OSINT": ["CodeCure"],
    },
    Cell.Difficulty.EASY: {
        "PWN": ["Y-CERT", "CodeCure"],
        "WEB": ["CodeCure", "SeKurity"],
        "REV": ["SWING", "Aegis"],
        "FORENSIC": ["Y-CERT", "MJSEC"],
        "WEB3": ["SeKurity"],
        "MISC": ["SWING"],
        "OSINT": ["MJSEC", "Aegis"],
    },
}

CHANCE_CARDS = [
    {
        "card_id": "card_reroll",
        "name": "주사위 다시 굴리기",
        "description": "굴리기 전 위치 기준으로 새로 굴려서 즉시 재이동합니다.",
        "effect": "RE_ROLL",
        "usage_timing": ChanceCard.UsageTiming.POST_ROLL,
    },
    {
        "card_id": "card_roll_twice_choose",
        "name": "주사위 2회 굴림 후 선택",
        "description": "주사위를 굴리기 전에 두 번 굴린 뒤 한 값을 골라 이동합니다.",
        "effect": "ROLL_TWICE_CHOOSE",
        "usage_timing": ChanceCard.UsageTiming.PRE_ROLL,
    },
    {
        "card_id": "card_move_offset",
        "name": "주변 칸 이동",
        "description": "굴린 뒤 확정 전인 도착 후보에서 앞/뒤 1~3칸 추가 이동합니다.",
        "effect": "MOVE_OFFSET",
        "usage_timing": ChanceCard.UsageTiming.POST_ROLL,
    },
    {
        "card_id": "card_free_travel",
        "name": "세계여행",
        "description": "원하는 칸으로 자유롭게 이동합니다.",
        "effect": "FREE_MOVE",
        "usage_timing": ChanceCard.UsageTiming.PRE_ROLL,
    },
    {
        "card_id": "card_extra_roll",
        "name": "주사위 보너스",
        "description": "주사위를 굴릴 수 있는 횟수가 1회 늘어납니다.",
        "effect": "GRANT_EXTRA_ROLL",
        "usage_timing": ChanceCard.UsageTiming.PRE_ROLL,
    },
]
