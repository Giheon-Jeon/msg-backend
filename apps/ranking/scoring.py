import math


def calculate_dynamic_score(initial_score, minimum_score, decay, solved_team_count):
    value = ((minimum_score - initial_score) / (decay**2)) * (
        solved_team_count**2
    ) + initial_score
    value = math.ceil(value)

    return min(initial_score, max(minimum_score, value))
