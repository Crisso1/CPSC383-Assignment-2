from aegis_game.stub import *
import heapq

"""
Assignment 2 - Multi-Agent Rescue (MAS)
Authors: Shayan Vaziri (UCID: 30174528), Christian Otalora (UCID: 10137257), Duncan McKay (UCID: 30177857)
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
ROLE_COORDINATOR = "coordinator"    # coordinator role constant string
ROLE_WORKER = "worker"              # worker role constant string

has_initialized = False
agent_role = None                   # will be either ROLE_COORDINATOR or ROLE_WORKER

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
allies = {}
arrived_last_round = {}              # Dict[int, tuple[int, int]]: agent_id -> (x, y)

# my planning state
my_target = None                     # tuple[int,int] | None
current_path = []                    # list[Direction]
current_path_goal = None             # tuple[int,int] | None
current_path_avoid_unknown = True    # bool
current_charger_xy = None

# ----------------------------
# Utilities
# ----------------------------
expanding_dirs = [
    Direction.NORTH, Direction.NORTHEAST, Direction.EAST, Direction.SOUTHEAST,
    Direction.SOUTH, Direction.SOUTHWEST, Direction.WEST, Direction.NORTHWEST,
]
lethal_cost = 500   # threshold for cells deemed impassable (lethal to agents)


def loc_to_xy(loc: Location) -> tuple[int, int]:    # convert Location to (x, y) tuple
    return (loc.x, loc.y)


def xy_to_loc(xy: tuple[int, int]) -> Location:     # convert (x, y) tuple to Location
    return Location(xy[0], xy[1])

def broadcast(msg: str) -> None:    # send a message to all agents
    try:
        send_message(msg, [])   
    except Exception:
        pass


def record_survivor(loc: Location, announce: bool = False) -> None:
    sxy = loc_to_xy(loc)
    if sxy in known_survivor_coords or sxy in completed_targets:    # survivor already known or saved
        return
    known_survivors.append(loc)     # add location object to list of known survivors
    known_survivor_coords.add(sxy)  # add the tuple version as a key
    if announce:
        broadcast(f"SURV|{sxy[0]}|{sxy[1]}")    # option to announce the discovery to other agents


def parse_messages() -> list[str]:
    msgs = []
    try:
        raw = read_messages()   # read recieved messages

        for m in raw:
            text = str(m)
            colon_index = text.find(':')
            if colon_index != -1:
                # Get everything after the colon and strip spaces and quotes
                msg = text[colon_index + 1:].strip()
                if msg.startswith('"') and msg.endswith('"'):   # strip surrounding quotes
                    msg = msg[1:-1]     
                msgs.append(msg)    # add to list of messages

    except Exception as e:
        return []

    return msgs     # return list of striped messages


def report_position():  # broadcast agent's position to the rest of the team
    loc = get_location()
    broadcast(f"POS|{get_id()}|{loc.x}|{loc.y}|{get_energy_level()}")


def ingest_pos_message(msg: str) -> None:
    try:
        _, sid, sx, sy, se = msg.split("|")     # format: POS|id|x|y|energy
        team_positions[int(sid)] = {            # keep track of other agents positions and energy level
            "loc": Location(int(sx), int(sy)),
            "energy": int(se),
        }
        known_agent_ids.add(int(sid))       # record seeing this agent
    except Exception:
        pass


def ingest_done_message(msg: str) -> None:
    try:
        _, sx, sy = msg.split("|")                  # DONE|x|y
        coord = (int(sx), int(sy))
        completed_targets.add(coord)                # mark task at that position as completed
        if coord in task_assignments:
            task_assignments[coord]["done"] = True  # update assignment table
    except Exception:
        pass


def ingest_assign_message(msg: str) -> None:
    try:
        parts = msg.split("|")                      # ASSIGN|x|y|req=2|agents=1,2
        sx, sy = int(parts[1]), int(parts[2])
        req = int(parts[3].split("=")[1])
        agents_data = parts[4].split("=")[1]
        assigned = [int(a) for a in agents_data.split(",") if a] # list of agents assigned there
    except Exception:
        return

    key = (sx, sy)              # tuple representing coords of the survivor task
    entry = task_assignments.get(key, {"done": False})  #  either retrieve existing task entry or create one marked not done for that location
    entry["required"] = req     # set required number of agents for task
    entry["assigned_ids"] = assigned    # list of agents assigned to task
    entry["done"] = entry.get("done", False) or key in completed_targets # mark as done if in list of completeled targets
    task_assignments[key] = entry   # store in global dictionary of task assignments


def ingest_hello_message(msg: str) -> None: # agents make themselves known to team
    try:
        _, sid = msg.split("|")
        known_agent_ids.add(int(sid))   # add agent to list of known ids
    except Exception:
        pass


def ingest_rubble_message(msg: str) -> None:
    global rubble_ready
    try:
        _, sx, sy, sid = msg.split("|")     # AT_RUBBLE|x|y|id
        coord = (int(sx), int(sy))
        agents = rubble_ready.setdefault(coord, set())
        agents.add(int(sid))                # record this agent's id as present at the rubble
        info = task_assignments.setdefault(coord, {"required": 1, "assigned_ids": [], "done": False})
        info["required"] = max(info.get("required", 1), 1)  # ensure the task expects more than one digger if neccesary
    except Exception:
        pass


def ingest_survivor_message(msg: str) -> None:
    try:
        _, sx, sy = msg.split("|")      # SURV|x|y
        record_survivor(Location(int(sx), int(sy)), announce=False)     # record location of survivor silently
    except Exception:
        pass


def process_incoming_messages() -> None:
    global rubble_ready
    msgs = parse_messages()  # parse the message
    for s in msgs:          # handle each method according to its prefix string
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
    known_agent_ids.add(get_id())   # ensure we are in known_ids
    team_positions[get_id()] = {
        "loc": get_location(),
        "energy": get_energy_level(),
    }   # update our position and energy for planning


def update_role_from_known_ids() -> None:
    global coordinator_id, agent_role
    if known_agent_ids:
        coordinator_id = min(known_agent_ids)   # lowest id will be assigned coordinator
    else:
        coordinator_id = get_id()               # if we are the only known agent left, we are the coordinator
    agent_role = ROLE_COORDINATOR if get_id() == coordinator_id else ROLE_WORKER    # make  role coordinator if assigned, otherwise worker


def share_visible_survivors() -> None:
    try:
        survs = get_survs()     # get survivors that are visible to us at tis tick
    except Exception:
        survs = []
    if survs is None:
        survs = []
    # reset snapshot each tick so all agents have consistent survivor list
    known_survivors.clear()
    known_survivor_coords.clear()
    for s in survs:
        record_survivor(s, announce=False)  # repopulate known survivor list without broadcasting


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

def chebyshev_heuristic(a: Location, b: Location) -> int: # Heuristic for pathfinding
    return max(abs(a.x - b.x), abs(a.y - b.y))


def loc_key(l: Location):   # helper to convert location to tuple key
    return (l.x, l.y)


def reconstruct_path(came_from, current_key):
    moves = []
    while current_key in came_from:
        previous_key, direction = came_from[current_key]
        moves.append(direction)
        current_key = previous_key
    moves.reverse()
    return moves    # get forward path


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


def estimate_path_cost_astar(src_xy: tuple[int, int], dst_xy: tuple[int, int]) -> int:  # get cost of the AStar path
    path_dirs = a_star(xy_to_loc(src_xy), xy_to_loc(dst_xy), avoid_unknown=False)
    if not path_dirs:
        return 1_000_000    # unreachable = large cost
    cur = xy_to_loc(src_xy)
    total = 0
    for d in path_dirs:
        nxt = cur.add(d)
        info = get_cell_info_at(nxt)
        step = info.move_cost if (info is not None and info.move_cost is not None) else 1
        total = total + step    # count up move costs along path
        cur = nxt
    return total


# ----------------------------
# Task creation and roles
# ----------------------------

def cell_requires_two_diggers(cell) -> bool: # check if rubble will require more than one digger
    try:
        top = cell.top_layer
        if isinstance(top, Rubble):
            # heuristic: strength>=2 may require two; if API exposes flag, use it
            try:
                if top.agents_required >= 2:
                    return True
            except AttributeError:
                pass
            try:
                if top.energy_required >= 2:
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
            return  # keep the current targer if assignment is the same or if no assignments
        # Target is no longer ours; clear it so we don't trail another rescuer.
        my_target = None
        for sxy, tinfo in task_assignments.items():
            if tinfo.get("done", False):
                continue
            if myid in tinfo.get("assigned_ids", []):
                my_target = sxy
                break
    if my_target is None and task_assignments:
        myid = get_id()
        here = loc_to_xy(get_location())

        # Step 1: See if we are already listed in any assignments
        for sxy, tinfo in task_assignments.items():
            if tinfo.get("done", False):
                continue
            if myid in tinfo.get("assigned_ids", []):
                my_target = sxy
                break

        # Step 2: If we aren't assigned to anything, fill a deficit using smarter criteria
        if my_target is None:
            deficits = []
            for sxy, tinfo in task_assignments.items():
                if tinfo.get("done", False):
                    continue
                assigned = tinfo.setdefault("assigned_ids", [])
                required = tinfo.get("required", 1)
                if len(assigned) < required and myid not in assigned:
                    # Only consider if a path exists
                    goal_loc = xy_to_loc(sxy)
                    path = a_star(get_location(), goal_loc, avoid_unknown=True)
                    if path:  # Only consider viable paths
                        deficits.append((sxy, path))

            if deficits:
                # Sort by path length instead of manhattan distance
                deficits.sort(key=lambda tup: len(tup[1]))
                choice, _ = deficits[0]
                my_target = choice
                task_assignments[choice]["assigned_ids"].append(myid)  # Explicitly claim it

    if my_target != previous:   # reset navigation if target changes
        current_path = []
        current_path_goal = None
        current_path_avoid_unknown = True


def coordinator_tick() -> bool:
    global task_assignments, scanned_survivors

    survivor_lookup: dict[tuple[int, int], Location] = {}
    for loc in known_survivors:
        survivor_lookup[loc_to_xy(loc)] = loc   # make a quick lookup from coords to Location

    action_used = False         # track if an action has been used this tick 
    rubble_coords = []          # coords where rubble found

    # identify rubble covering survivors
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
        if isinstance(top, Rubble): # if the top layer is rubble, store coords
            rubble_coords.append(coord)

    # scan survivors covered in rubble if not already done
    for coord in rubble_coords:
        if coord in scanned_survivors:
            continue
        if coord not in survivor_lookup:
            continue
        if action_used: # only 1 per tick
            break
        loc = survivor_lookup[coord]
        cell = get_cell_info_at(loc)
        if cell is None:
            continue
        top = isinstance(top, Survivor)
        if isinstance(top, Survivor):   # if top  is just a survivor, mark as scanned
            scanned_survivors.add(coord)
            continue
        try:
            drone_scan(loc) # scan
        except Exception:
            pass
        scanned_survivors.add(coord)
        action_used = True  # mark action as used if having to drone scan

    # build new task list based on this info
    new_tasks: dict[tuple[int, int], dict] = {}
    for coord, loc in survivor_lookup.items():
        if coord in completed_targets:
            continue
        cell = get_cell_info_at(loc)
        required = 1    # require 1 agent by default
        try:
            top = cell.top_layer if cell is not None else None  
        except AttributeError:
            top = None
        if isinstance(top, Rubble):
            required = top.agents_required if cell_requires_two_diggers(cell) else 1 # determine number of agents needed to clear rubble
        new_tasks[coord] = {        #  create task
            "required": required,
            "assigned_ids": [],
            "loc": loc,
        }

    # create snapshot of all the agents and their energy levels and location
    snapshots: dict[int, dict[str, object]] = {}
    for aid in known_agent_ids | {get_id()}:
        if aid == get_id(): # include coordinator
            snapshots[aid] = {"loc": get_location(), "energy": get_energy_level()}
        else:
            data = team_positions.get(aid)  # include info of known teammates
            if data is not None:
                snapshots[aid] = {
                    "loc": data.get("loc"),
                    "energy": data.get("energy"),
                }

    # get cost for agent-task pairings
    pairings = []
    for aid, info in snapshots.items():
        loc = info.get("loc")
        if loc is None:
            continue
        start_xy = loc_to_xy(loc)
        for coord in new_tasks.keys():
            cost = estimate_path_cost_astar(start_xy, coord)    # get cost to that location of task
            if cost >= 1_000_000:   # skip unreachable tasks
                continue
            pairings.append((cost, aid, coord)) # add to list of possible pairings

    pairings.sort(key=lambda item: item[0]) # sort pairings by cost and choose the first (cheapest)
    taken_agents: set[int] = set()
    for _, aid, coord in pairings:  # assign tasks
        if aid in taken_agents:
            continue
        task = new_tasks.get(coord)
        if task is None:
            continue
        if len(task["assigned_ids"]) >= task["required"]:
            continue
        task["assigned_ids"].append(aid)
        taken_agents.add(aid)

    # update task assignments and broadcast to agents
    task_assignments.clear()
    for coord, task in new_tasks.items():
        task_assignments[coord] = {
            "required": task["required"],
            "assigned_ids": list(task["assigned_ids"]),
            "done": False,
        }
        msg = f"ASSIGN|{coord[0]}|{coord[1]}|req={task['required']}|agents=" + ",".join(str(a) for a in task["assigned_ids"]) # broadcast assignment
        broadcast(msg)

    ensure_self_assignment()    # ensure the coordinator has an assignment
    return action_used


def worker_tick() -> None:
    ensure_self_assignment()    # workers should just ensure they have an assignment


# ----------------------------
# Acting helpers
# ----------------------------
def need_recharge_for(target_xy: tuple[int, int]) -> tuple[bool, tuple[int, int] | None]: # returns whether to recharge and a chosen charger
    """Return a tuple indicating whether to recharge and the chosen charger (None when unavailable)."""
    here_xy = loc_to_xy(get_location())
    est = estimate_path_cost_astar(here_xy, target_xy)  # cost to taget
    if est >= 1_000_000:
        return (False, None)    # if unreachable, dont bother recharging
    if get_energy_level() >= est:   # have enough energy
        return (False, None)

    best_charger = None
    best_cost = None
    for charger in get_charging_cells() or []:  # iterate through chargers
        ch_xy = loc_to_xy(charger)
        cost = estimate_path_cost_astar(here_xy, ch_xy) # cost to charger
        if cost >= 1_000_000:   # unreachable
            continue
        if cost > get_energy_level():   # cant reach this charger
            continue
        if best_cost is None or cost < best_cost:   # find best charger
            best_cost = cost
            best_charger = ch_xy

    return (best_charger is not None, best_charger) # (whether_to_recharge, ideal_charger)


def act_move_towards(dst_xy: tuple[int, int], avoid_unknown: bool = True) -> bool:
    global current_path, current_path_goal, current_path_avoid_unknown
    goal_loc = xy_to_loc(dst_xy)

    # recompute path if target has changed ot if current path is empty
    if current_path_goal != dst_xy or not current_path:
        path = a_star(get_location(), goal_loc, avoid_unknown=avoid_unknown)    # compuute path
        path_avoid_unknown = avoid_unknown
        if avoid_unknown and not path:
            path = a_star(get_location(), goal_loc, avoid_unknown=False)    # fallback for allowing unknowns
            path_avoid_unknown = False
        current_path = path
        current_path_goal = dst_xy
        current_path_avoid_unknown = path_avoid_unknown

    if not current_path:    # no avaiable path
        return False
    next_dir = current_path.pop(0)  # take next step
    nxt = get_location().add(next_dir)  # compute location after move

    if not on_map(nxt): # ensure next move is on the map
        current_path = []
        return False
    
    info = get_cell_info_at(nxt) # get cell information for next step
    if info is None and current_path_avoid_unknown: # stop if the cell info is missing and are avoiding unknowns
        current_path = []
        return False
    
    try:
        if info is not None and info.is_killer_cell():  # check if cell is a killer
            current_path = []
            return False
    except Exception:
        pass

    if info is not None and info.move_cost is not None and info.move_cost >= lethal_cost:   # check for high move cost
        current_path = []
        return False
    if current_path_avoid_unknown and info is not None and info.move_cost is None:  # avoid unknown cells if specified
        current_path = []
        return False
    move(next_dir)  # execute the move
    return True


def synchronize_and_dig(target_xy: tuple[int, int], required: int) -> bool: 
    global allies, rubble_ready
    here = get_location()
    if (here.x, here.y) != target_xy:   # make sure you are on target
        return False
    cell = get_cell_info_at(here)
    if cell is None:
        return False
    if isinstance(cell.top_layer, Survivor):    # if top is a survivor, rescue them
        save()
        broadcast(f"DONE|{target_xy[0]}|{target_xy[1]}")    # tell team that the task is done
        if target_xy in task_assignments:
            task_assignments[target_xy]["done"] = True
        return True
    if isinstance(cell.top_layer, Rubble):  # If rubble on top layer
        if required <= 1:   # solo dig
            dig()
            return True
        broadcast(f"AT_RUBBLE|{target_xy[0]}|{target_xy[1]}|{get_id()}")    # declare presence at rubble
        allies = rubble_ready.setdefault(target_xy, set())
        allies.add(get_id())    # add self to allies list
        if len(allies) >= required:
            dig()       # dig when required amount of agents are there to dig
            return True
        return True
    return False


# ----------------------------
# Main loop
# ----------------------------
def think() -> None:
    global has_initialized, agent_role, my_target, coordinator_id
    global current_path, current_path_goal, current_path_avoid_unknown
    global current_charger_xy

    if not has_initialized:
        # start as provisional coordinator; will yield to lower id once discovered
        coordinator_id = get_id()
        agent_role = ROLE_COORDINATOR
        known_agent_ids.add(get_id())
        broadcast(f"HELLO|{get_id()}")  # announce yourself to team
        report_position()               # and broadcast your position/energy
        has_initialized = True
        return

    # every tick, update your stats, handle message from other agents, update local team member positions and known ids, 
    # refresh local survior snapshot, and compute coordinator based on lowest id
    report_position()
    process_incoming_messages()
    update_self_snapshot()
    share_visible_survivors()
    update_role_from_known_ids()

    here_xy = loc_to_xy(get_location()) # current location

    action_used = False     # one action per tick
    if agent_role == ROLE_COORDINATOR:  # determine action based on role
        action_used = coordinator_tick()   
    else:
        worker_tick()

    if action_used: # if action used, ends turn
        return

    if my_target and my_target in completed_targets: # if target is complete, clear target and path goal
        my_target = None
        current_path = []
        current_path_goal = None
        current_path_avoid_unknown = True

    if my_target is None:   # if you have no target, try to get a target
        ensure_self_assignment()

    if my_target is None:
        try:
            survs = get_survs()
        except Exception:
            survs = []
        if survs:
            my_target = loc_to_xy(survs[0]) # as a fallback, target the first survivor visible
        else:
            return

    
    needs_recharge, charger_xy = need_recharge_for(my_target) # see if recharge required

    if here_xy == current_charger_xy:   # if standing on selected charger, use it
            recharge()
            current_charger_xy = None  # Clear after recharging
            return
    
    if needs_recharge:
        # Set charger only if not already set or if target changed
        if current_charger_xy is None:
            current_charger_xy = charger_xy

        if current_charger_xy is None:
            move(Direction.CENTER)  # wait in place if no reachable charger
            return
        
        if act_move_towards(current_charger_xy, avoid_unknown=True):
            return  # moved towards charger for action this tick

        move(Direction.CENTER)  # otherwise wait in place
        return

    required = task_assignments.get(my_target, {}).get("required", 1)
    if (get_location().x, get_location().y) != my_target:   # if not at target, move towards it this tick
        if act_move_towards(my_target, avoid_unknown=True):
            return
        return

    if synchronize_and_dig(my_target, required):    # if save/dig action performed
        return  
    
    my_target = None    # clear target and current path goal
    current_path = []
    current_path_goal = None
    current_path_avoid_unknown = True
    return
