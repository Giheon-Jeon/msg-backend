def resolve_last_solved_at(
    jeopardy_solved_at,
    koth_solved_at,
    signature_solved_at=None,
):
    if jeopardy_solved_at is not None:
        return jeopardy_solved_at

    if koth_solved_at is not None:
        return koth_solved_at

    return signature_solved_at


def build_team_ranking(team_data, limit=None):
    result = []

    for row in team_data:
        total = (
            row["jeopardy_score"]
            + row["koth_score"]
            + row.get("signature_score", 0)
            + row.get("line_score", 0)
        )

        last_solved_at = resolve_last_solved_at(
            row["jeopardy_solved_at"],
            row["koth_solved_at"],
            row.get("signature_solved_at"),
        )

        result.append(
            {
                "team_id": row["team_id"],
                "team_name": row["team_name"],
                "team_score": total,
                "mileage": row["mileage"],
                "last_solved_at": last_solved_at,
            }
        )

    result.sort(key=sort_key)

    if limit is None:  # /ranking/me
        top = result
    else:
        top = result[:limit]  # /ranking

    rank = 1
    for row in top:
        row["rank"] = rank
        rank = rank + 1

    return top


def sort_key(row):
    score = row["team_score"] * -1
    last_solved_at = row["last_solved_at"]

    if last_solved_at is None:
        no_solve = True
        solved_at = 0
    else:
        no_solve = False
        solved_at = last_solved_at.timestamp()

    return (score, no_solve, solved_at, row["team_id"])


def member_sort_key(row):
    score = row["user_score"] * -1

    last_solved_at = row["last_solved_at"]

    if last_solved_at is None:
        no_solve = True
        solved_at = 0
    else:
        no_solve = False
        solved_at = last_solved_at.timestamp()

    return (score, no_solve, solved_at, row["user_id"])


def build_member_ranking(member_data):
    # 개인 순위 목록
    result = []

    for row in member_data:
        result.append(
            {
                "user_id": row["user_id"],
                "nickname": row["nickname"],
                "team_id": row["team_id"],
                "team_name": row["team_name"],
                "user_score": row["user_score"],
                "solved_count": row["solved_count"],
                "last_solved_at": row["last_solved_at"],
            }
        )

    result.sort(key=member_sort_key)

    rank = 1
    for row in result:
        row["rank"] = rank
        rank = rank + 1

    return result
