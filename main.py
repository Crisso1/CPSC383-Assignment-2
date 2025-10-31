from aegis_game.stub import *
import heapq

"""
Assignment 2 - Multi-Agent Rescue (MAS)
Authors: Shayan Vaziri (UCID: 30174528) and Group
Date: 2025-10-29
Course: CPSC 383, Fall 2025, Tutorial L01 T04

Design notes:
- One coordinator (lowest observed id) scans survivor tiles with drone and assigns tasks.
- Agents broadcast positions/energy each round, accept assignments, navigate via A*.
- Simple charging detours if estimated path energy exceeds current energy.
- Rubble coordination: if tile likely needs two, agents rendezvous and dig together.


"""

# ----------------------------
# Module-level agent state (persisted across rounds)
# ----------------------------
ROLE_COORDINATOR = "coordinator"
ROLE_WORKER = "worker"

has_initialized = False
agent_role = None

# knowledge and coordination
known_survivors = []                 # list[Location]
known_survivor_coords = set()        # set[(x,y)]
pending_forces = set()               # survivors awaiting additional agent
task_assignments = {}                # dict[(x,y)] -> {required:int, assigned_ids:list[int], done:bool}
completed_targets = set()            # set[(x,y)]
team_positions = {}                  # dict[id] -> {loc:Location, energy:int}
known_agent_ids = set()              # set[int]
coordinator_id = None                # int | None
scanned_survivors = set()            # set[(x,y)] of drone-scanned tiles
rubble_ready = {}                    # dict[(x,y)] -> set[int]

# my planning state
my_target = None                     # tuple[int,int] | None
current_path = []                    # list[Direction]
current_path_goal = None             # tuple[int,int] | None
current_path_avoid_unknown = True    # bool

# ----------------------------
# Utilities
# ----------------------------
expanding_dirs = [
    Direction.NORTH, Direction.NORTHEAST, Direction.EAST, Direction.SOUTHEAST,
    Direction.SOUTH, Direction.SOUTHWEST, Direction.WEST, Direction.NORTHWEST,
]
lethal_cost = 500


def loc_to_xy(loc: Location) -> tuple[int, int]:
    return (loc.x, loc.y)


def xy_to_loc(xy: tuple[int, int]) -> Location:
    return Location(xy[0], xy[1])


def manhattan(a: tuple[int, int], b: tuple[int, int]) -> int:
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


def broadcast(msg: str) -> None:
    try:
        send_message(msg, [])
    except Exception:
        pass


def record_survivor(loc: Location, announce: bool = False) -> None:
    sxy = loc_to_xy(loc)
    if sxy in known_survivor_coords or sxy in completed_targets:
        return
    known_survivors.append(loc)
    known_survivor_coords.add(sxy)
    if announce:
        broadcast(f"SURV|{sxy[0]}|{sxy[1]}")


def parse_messages() -> list[str]:
    msgs = []
    try:
        for m in read_messages():
            try:
                if isinstance(m, str):
                    msgs.append(m)
                else:
                    try:
                        msgs.append(str(m.text))
                    except AttributeError:
                        msgs.append(str(m))
            except Exception:
                continue
    except Exception:
        return []
    return msgs


def report_position():
    loc = get_location()
    broadcast(f"POS|{get_id()}|{loc.x}|{loc.y}|{get_energy_level()}")


def ingest_pos_message(msg: str) -> None:
    try:
        _, sid, sx, sy, se = msg.split("|")
        team_positions[int(sid)] = {
            "loc": Location(int(sx), int(sy)),
            "energy": int(se),
        }
        known_agent_ids.add(int(sid))
    except Exception:
        pass


def ingest_done_message(msg: str) -> None:
    try:
        _, sx, sy = msg.split("|")
        coord = (int(sx), int(sy))
        completed_targets.add(coord)
        if coord in task_assignments:
            task_assignments[coord]["done"] = True
    except Exception:
        pass


def ingest_assign_message(msg: str) -> None:
    try:
        parts = msg.split("|")
        sx, sy = int(parts[1]), int(parts[2])
        req = int(parts[3].split("=")[1])
        agents_data = parts[4].split("=")[1]
        assigned = [int(a) for a in agents_data.split(",") if a]
    except Exception:
        return

    key = (sx, sy)
    entry = task_assignments.get(key, {"done": False})
    entry["required"] = req
    entry["assigned_ids"] = assigned
    entry["done"] = entry.get("done", False) or key in completed_targets
    task_assignments[key] = entry


def ingest_hello_message(msg: str) -> None:
    try:
        _, sid = msg.split("|")
        known_agent_ids.add(int(sid))
    except Exception:
        pass


def ingest_rubble_message(msg: str) -> None:
    try:
        _, sx, sy, sid = msg.split("|")
        coord = (int(sx), int(sy))
        agents = rubble_ready.setdefault(coord, set())
        agents.add(int(sid))
        info = task_assignments.setdefault(coord, {"required": 1, "assigned_ids": [], "done": False})
        info["required"] = max(info.get("required", 1), 2)
    except Exception:
        pass


def ingest_survivor_message(msg: str) -> None:
    try:
        _, sx, sy = msg.split("|")
        record_survivor(Location(int(sx), int(sy)), announce=False)
    except Exception:
        pass


def process_incoming_messages() -> None:
    global rubble_ready
    rubble_ready = {}

    msgs = parse_messages()
    for s in msgs:
        if s.startswith("POS|"):
            ingest_pos_message(s)
            continue
        if s.startswith("ASSIGN|"):
            ingest_assign_message(s)
            continue
        if s.startswith("DONE|"):
            ingest_done_message(s)
            continue
        if s.startswith("HELLO|"):
            ingest_hello_message(s)
            continue
        if s.startswith("AT_RUBBLE|"):
            ingest_rubble_message(s)
            continue
        if s.startswith("SURV|"):
            ingest_survivor_message(s)
            continue


def update_self_snapshot() -> None:
    known_agent_ids.add(get_id())
    team_positions[get_id()] = {
        "loc": get_location(),
        "energy": get_energy_level(),
    }


def update_role_from_known_ids() -> None:
    global coordinator_id, agent_role
    if known_agent_ids:
        coordinator_id = min(known_agent_ids)
    else:
        coordinator_id = get_id()
    agent_role = ROLE_COORDINATOR if get_id() == coordinator_id else ROLE_WORKER


def share_visible_survivors() -> None:
    try:
        survs = get_survs()
    except Exception:
        survs = []
    if survs is None:
        survs = []
    # reset snapshot each tick so all agents have consistent survivor list
    known_survivors.clear()
    known_survivor_coords.clear()
    for s in survs:
        record_survivor(s, announce=False)


def nearest_charger(from_xy: tuple[int, int]) -> tuple[int, int] | None:
    chargers = get_charging_cells()
    if not chargers:
        return None
    chargers_xy = [loc_to_xy(c) for c in chargers]
    chargers_xy.sort(key=lambda cxy: manhattan(from_xy, cxy))
    return chargers_xy[0]


# ----------------------------
# Pathfinding (A*)
# ----------------------------
def info_is_killer(ci) -> bool:
    # Robust killer detection across possible API variants
    try:
        if ci.is_killer_cell():
            return True
    except AttributeError:
        pass
    try:
        top = ci.top_layer
    except AttributeError:
        top = None
    if top is not None:
        try:
            if bool(top.is_killer):
                return True
        except AttributeError:
            pass
        if "Killer" in str(type(top)):
            return True
    return False


def diagonal_blocked(cur: Location, nxt: Location, avoid_unknown: bool) -> bool:
    # If moving diagonally, ensure orthogonal neighbors are safe/known
    if cur.x != nxt.x and cur.y != nxt.y:
        ortho1 = Location(cur.x, nxt.y)
        ortho2 = Location(nxt.x, cur.y)
        for o in (ortho1, ortho2):
            if not on_map(o):
                return True
            ci = get_cell_info_at(o)
            if ci is None:
                return True
            if info_is_killer(ci):
                return True
            if avoid_unknown and ci.move_cost is None:
                return True
    return False

def chebyshev_heuristic(a: Location, b: Location) -> int:
    return max(abs(a.x - b.x), abs(a.y - b.y))


def loc_key(l: Location):
    return (l.x, l.y)


def reconstruct_path(came_from, current_key):
    moves = []
    while current_key in came_from:
        previous_key, direction = came_from[current_key]
        moves.append(direction)
        current_key = previous_key
    moves.reverse()
    return moves


def a_star(start: Location, goal: Location, avoid_unknown: bool) -> list[Direction]:
    # User-specified A* (preserved)
    if not on_map(goal):
        return []

    start_k, goal_k = loc_key(start), loc_key(goal)
    g_score = {start_k: 0}
    came_from = {}
    key_to_loc = {start_k: start, goal_k: goal}
    open_set = []
    seq = 0
    h0 = chebyshev_heuristic(start, goal)
    heapq.heappush(open_set, (h0, h0, seq, start_k))

    while open_set:
        _, _, _, current_key = heapq.heappop(open_set)
        current_loc = key_to_loc[current_key]

        if current_key == goal_k:
            return reconstruct_path(came_from, current_key)

        for d in expanding_dirs:
            nbr_loc = current_loc.add(d)
            if not on_map(nbr_loc):
                continue

            nbr_k = loc_key(nbr_loc)
            key_to_loc[nbr_k] = nbr_loc
            info = get_cell_info_at(nbr_loc)

            # error checking
            if info is None:
                if avoid_unknown:
                    continue
                step_cost = 1
            else:
                try:
                    if info.is_killer_cell():
                        continue
                except Exception:
                    # if API differs, skip only on explicit killer support
                    pass
                if info.move_cost is not None and info.move_cost >= lethal_cost:
                    continue
                if avoid_unknown and info.move_cost is None:
                    continue
                step_cost = info.move_cost if info.move_cost is not None else 1

            tentative_g = g_score[current_key] + step_cost

            if nbr_k not in g_score or tentative_g < g_score[nbr_k]:
                g_score[nbr_k] = tentative_g
                h = chebyshev_heuristic(nbr_loc, goal)
                f = tentative_g + h
                came_from[nbr_k] = (current_key, d)
                seq = seq + 1
                heapq.heappush(open_set, (f, h, seq, nbr_k))

    return []


def estimate_path_cost_astar(src_xy: tuple[int, int], dst_xy: tuple[int, int]) -> int:
    path_dirs = a_star(xy_to_loc(src_xy), xy_to_loc(dst_xy), avoid_unknown=False)
    if not path_dirs:
        return 1_000_000
    cur = xy_to_loc(src_xy)
    total = 0
    for d in path_dirs:
        nxt = cur.add(d)
        info = get_cell_info_at(nxt)
        step = info.move_cost if (info is not None and info.move_cost is not None) else 1
        total = total + step
        cur = nxt
    return total


# ----------------------------
# Task creation and roles
# ----------------------------
def cell_requires_two_diggers(cell) -> bool:
    try:
        top = cell.top_layer
        if isinstance(top, Rubble):
            # heuristic: strength>=2 may require two; if API exposes flag, use it
            try:
                if bool(top.requires_two_agents):
                    return True
            except AttributeError:
                pass
            try:
                if int(top.strength) >= 2:
                    return True
            except AttributeError:
                pass
    except Exception:
        pass
    return False


def ensure_self_assignment() -> None:
    """Pick a survivor if we are listed in its assignment, or fill gaps if any remain."""
    global my_target, current_path, current_path_goal, current_path_avoid_unknown
    previous = my_target
    myid = get_id()
    if my_target and my_target in task_assignments and not task_assignments[my_target].get("done", False):
        if myid in task_assignments[my_target].get("assigned_ids", []) or not task_assignments[my_target].get("assigned_ids", []):
            return
        # Target is no longer ours; clear it so we don't trail another rescuer.
        my_target = None
        for sxy, tinfo in task_assignments.items():
            if tinfo.get("done", False):
                continue
            if myid in tinfo.get("assigned_ids", []):
                my_target = sxy
                break
    if my_target is None and task_assignments:
        here = loc_to_xy(get_location())
        deficits = []  # survivors that still need extra rescuers this tick
        for sxy, tinfo in task_assignments.items():
            if tinfo.get("done", False):
                continue
            assigned = tinfo.setdefault("assigned_ids", [])
            required = tinfo.get("required", 1)
            if len(assigned) < required and myid not in assigned:
                deficits.append(sxy)
        if deficits:
            deficits.sort(key=lambda c: manhattan(here, c))
            choice = deficits[0]
            my_target = choice

    if my_target != previous:
        current_path = []
        current_path_goal = None
        current_path_avoid_unknown = True


def coordinator_tick() -> bool:
    global task_assignments, scanned_survivors

    survivor_lookup: dict[tuple[int, int], Location] = {}
    for loc in known_survivors:
        survivor_lookup[loc_to_xy(loc)] = loc

    action_used = False
    rubble_coords = []
    for coord, loc in survivor_lookup.items():
        if coord in completed_targets:
            continue
        cell = get_cell_info_at(loc)
        if cell is None:
            continue
        try:
            top = cell.top_layer
        except AttributeError:
            top = None
        if isinstance(top, Rubble):
            rubble_coords.append(coord)

    for coord in rubble_coords:
        if coord in scanned_survivors:
            continue
        if coord not in survivor_lookup:
            continue
        if action_used:
            break
        loc = survivor_lookup[coord]
        try:
            drone_scan(loc)
        except Exception:
            pass
        scanned_survivors.add(coord)
        action_used = True

    new_tasks: dict[tuple[int, int], dict] = {}
    for coord, loc in survivor_lookup.items():
        if coord in completed_targets:
            continue
        cell = get_cell_info_at(loc)
        required = 1
        try:
            top = cell.top_layer if cell is not None else None
        except AttributeError:
            top = None
        if isinstance(top, Rubble):
            required = 2 if cell_requires_two_diggers(cell) else 1
        new_tasks[coord] = {
            "required": required,
            "assigned_ids": [],
            "loc": loc,
        }

    snapshots: dict[int, dict[str, object]] = {}
    for aid in known_agent_ids | {get_id()}:
        if aid == get_id():
            snapshots[aid] = {"loc": get_location(), "energy": get_energy_level()}
        else:
            data = team_positions.get(aid)
            if data is not None:
                snapshots[aid] = {
                    "loc": data.get("loc"),
                    "energy": data.get("energy"),
                }

    pairings = []
    for aid, info in snapshots.items():
        loc = info.get("loc")
        if loc is None:
            continue
        start_xy = loc_to_xy(loc)
        for coord in new_tasks.keys():
            cost = estimate_path_cost_astar(start_xy, coord)
            if cost >= 1_000_000:
                continue
            pairings.append((cost, aid, coord))

    pairings.sort(key=lambda item: item[0])
    taken_agents: set[int] = set()
    for _, aid, coord in pairings:
        if aid in taken_agents:
            continue
        task = new_tasks.get(coord)
        if task is None:
            continue
        if len(task["assigned_ids"]) >= task["required"]:
            continue
        task["assigned_ids"].append(aid)
        taken_agents.add(aid)

    task_assignments.clear()
    for coord, task in new_tasks.items():
        task_assignments[coord] = {
            "required": task["required"],
            "assigned_ids": list(task["assigned_ids"]),
            "done": False,
        }
        msg = f"ASSIGN|{coord[0]}|{coord[1]}|req={task['required']}|agents=" + ",".join(str(a) for a in task["assigned_ids"])
        broadcast(msg)

    ensure_self_assignment()
    return action_used


def worker_tick() -> None:
    ensure_self_assignment()


# ----------------------------
# Acting helpers
# ----------------------------
def need_recharge_for(target_xy: tuple[int, int]) -> tuple[bool, tuple[int, int] | None]:
    """Return a tuple indicating whether to recharge and the chosen charger (None when unavailable)."""
    here_xy = loc_to_xy(get_location())
    est = estimate_path_cost_astar(here_xy, target_xy)
    if est >= 1_000_000:
        return (False, None)
    buffer = 6
    dig_cost = 1
    save_cost = 1
    required = est + buffer + dig_cost + save_cost
    if get_energy_level() >= required:
        return (False, None)

    best_charger = None
    best_cost = None
    for charger in get_charging_cells() or []:
        ch_xy = loc_to_xy(charger)
        cost = estimate_path_cost_astar(here_xy, ch_xy)
        if cost >= 1_000_000:
            continue
        if cost > get_energy_level():
            continue
        if best_cost is None or cost < best_cost:
            best_cost = cost
            best_charger = ch_xy

    return (best_charger is not None, best_charger)


def act_move_towards(dst_xy: tuple[int, int], avoid_unknown: bool = True) -> bool:
    global current_path, current_path_goal, current_path_avoid_unknown
    goal_loc = xy_to_loc(dst_xy)
    if current_path_goal != dst_xy or not current_path:
        path = a_star(get_location(), goal_loc, avoid_unknown=avoid_unknown)
        path_avoid_unknown = avoid_unknown
        if avoid_unknown and not path:
            path = a_star(get_location(), goal_loc, avoid_unknown=False)
            path_avoid_unknown = False
        current_path = path
        current_path_goal = dst_xy
        current_path_avoid_unknown = path_avoid_unknown
    if not current_path:
        return False
    next_dir = current_path.pop(0)
    nxt = get_location().add(next_dir)
    if not on_map(nxt):
        current_path = []
        return False
    info = get_cell_info_at(nxt)
    if info is None and current_path_avoid_unknown:
        current_path = []
        return False
    try:
        if info is not None and info.is_killer_cell():
            current_path = []
            return False
    except Exception:
        pass
    if info is not None and info.move_cost is not None and info.move_cost >= lethal_cost:
        current_path = []
        return False
    if current_path_avoid_unknown and info is not None and info.move_cost is None:
        current_path = []
        return False
    move(next_dir)
    return True


def synchronize_and_dig(target_xy: tuple[int, int], required: int) -> bool:
    here = get_location()
    if (here.x, here.y) != target_xy:
        return False
    cell = get_cell_info_at(here)
    if cell is None:
        return False
    if isinstance(cell.top_layer, Survivor):
        save()
        broadcast(f"DONE|{target_xy[0]}|{target_xy[1]}")
        if target_xy in task_assignments:
            task_assignments[target_xy]["done"] = True
        return True
    if isinstance(cell.top_layer, Rubble):
        if required <= 1:
            dig()
            return True
        broadcast(f"AT_RUBBLE|{target_xy[0]}|{target_xy[1]}|{get_id()}")
        allies = rubble_ready.setdefault(target_xy, set())
        allies.add(get_id())
        if len(allies) >= required:
            dig()
            return True
        return True
    return False


# ----------------------------
# Main loop
# ----------------------------
def think() -> None:
    global has_initialized, agent_role, my_target, coordinator_id
    global current_path, current_path_goal, current_path_avoid_unknown

    if not has_initialized:
        # start as provisional coordinator; will yield to lower id once discovered
        coordinator_id = get_id()
        agent_role = ROLE_COORDINATOR
        known_agent_ids.add(get_id())
        broadcast(f"HELLO|{get_id()}")
        report_position()
        if agent_role == ROLE_COORDINATOR:
            try:
                for s in get_survs():
                    drone_scan(s)
            except Exception:
                pass
        has_initialized = True
        return

    report_position()
    process_incoming_messages()
    update_self_snapshot()
    share_visible_survivors()
    update_role_from_known_ids()

    action_used = False
    if agent_role == ROLE_COORDINATOR:
        action_used = coordinator_tick()
    else:
        worker_tick()

    if action_used:
        return

    if my_target and my_target in completed_targets:
        my_target = None
        current_path = []
        current_path_goal = None
        current_path_avoid_unknown = True

    if my_target is None:
        ensure_self_assignment()

    if my_target is None:
        try:
            survs = get_survs()
        except Exception:
            survs = []
        if survs:
            my_target = loc_to_xy(survs[0])
        else:
            return

    needs_recharge, charger_xy = need_recharge_for(my_target)
    if needs_recharge:
        here_xy = loc_to_xy(get_location())
        if charger_xy is None:
            move(Direction.CENTER)
            return
        if here_xy == charger_xy:
            recharge()
            return
        if act_move_towards(charger_xy, avoid_unknown=True):
            return
        move(Direction.CENTER)
        return

    required = task_assignments.get(my_target, {}).get("required", 1)
    if (get_location().x, get_location().y) != my_target:
        if act_move_towards(my_target, avoid_unknown=True):
            return
        return

    if synchronize_and_dig(my_target, required):
        return
    my_target = None
    current_path = []
    current_path_goal = None
    current_path_avoid_unknown = True
    return
