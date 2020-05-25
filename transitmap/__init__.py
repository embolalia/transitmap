from collections import defaultdict, namedtuple
from itertools import tee
import logging
import math

from gtfslib.dao import Dao
from gtfslib import orm
import requests
import svgwrite

# https://www.cambooth.net/how-to-design-a-transit-diagram/

GRID = 10

# http://web.mta.info/developers/developer-data-terms.html#data
# see also: https://transitfeeds.com/l/31-united-states
nyct_endpoint = 'http://web.mta.info/developers/data/nyct/subway/google_transit.zip'


def download():
    with open('mta_key.txt') as f:
        NYMTA_API_KEY = f.read().strip()
    with requests.get(
        nyct_endpoint,
        headers={'x-api-key': NYMTA_API_KEY},
    ) as response:
        response.raise_for_status()
        with open('nyct.zip', 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
            f.flush()


def load():
    logging.info('Loading GTFS file')
    dao = Dao('db.sqlite')
    #dao.load_gtfs('nyct.zip', feed_id='nyct')
    return dao


StationService = namedtuple('StationService', 'next_station,next_direction,prev_station,prev_direction,route,subroute,stops_here')


class Point(namedtuple('Point', 'x,y')):
    def __add__(self, other):
        return Point(self.x + other.x, self.y + other.y)

    def __sub__(self, other):
        return Point(self.x - other.x, self.y - other.y)

    def __mul__(self, other):
        return Point(self.x * other, self.y * other)

    def __neg__(self):
        return Point(-self.x, -self.y)


def _coords(stop):
    # lat is 40.5 - 40.9
    lat = (stop.stop_lat - 40.5) * -1000
    # lon is -74.2 - -73.75
    lon = (stop.stop_lon + 74.5) * 1000
    return Point(lon, lat)


class Station:
    def __init__(self, stop):
        self._gtfs_stop = stop
        self.coords = _coords(self._gtfs_stop)
        self.map_coords = None
        self.station_services = []

    def __hash__(self):
        return hash(self._gtfs_stop.stop_id)

    def path_to(self, target, previous=None, max_steps=8):
        max_steps -= 1
        if max_steps == 0:
            return
        if self == target:
            return
        for next_ in self.station_services:
            stop = next_.next_station
            if stop == previous:
                continue
            if stop == target:
                return [next_]
            path = stop.path_to(target, previous=self, max_steps=max_steps)
            if path:
                return [next_] + path

    @staticmethod
    def calculate_direction(point1, point2):
        diff = point2 - point1

        # yay manhattan
        offset = math.radians(29)
        true_angle = math.atan(diff.y / (diff.x or 0.000001))
        if diff.x < 0:
            true_angle += math.pi
        map_angle = (true_angle - offset) % (2* math.pi)
        sector = 8 * map_angle / math.pi

        if sector < 1 or sector >= 15:
            direction = Point(1, 0)
        elif sector < 3:
            direction = Point(1, 1)
        elif sector < 5:
            direction = Point(0, 1)
        elif sector < 7:
            direction = Point(-1, 1)
        elif sector < 9:
            direction = Point(-1, 0)
        elif sector < 11:
            direction = Point(-1, -1)
        elif sector < 13:
            direction = Point(0, -1)
        else:
            direction = Point(1, -1)
        return direction

    def _route_express(self, route, subroute, prev_station, prev_direction, alt_path):
        self.station_services.append(
            StationService(
                alt_path[0].next_station, alt_path[0].next_direction,
                prev_station, prev_direction,
                route, subroute, True
            )
        )

        station = alt_path[0].next_station
        for node in alt_path[1:-2]:
            station.station_services.append(
                StationService(
                    node.next_station, node.next_direction,
                    node.prev_station, node.prev_direction,
                    route, subroute, False
                )
            )
            station = node.next_station
        return alt_path[-1].prev_station, alt_path[-1].prev_direction

    def add_service(self, route, subroute, prev_station, prev_direction, next_station):
        next_direction = None
        if next_station:
            next_direction = self.calculate_direction(self.coords, next_station.coords)
            alt_path = self.path_to(next_station)
            if not alt_path or len(alt_path) < 2:
                pass
            elif alt_path[1].route != route: # i.e. follow a local
                # Returns because it changes what the previous stop/direction is
                return self._route_express(route, subroute, prev_station, prev_direction, alt_path)
            else: # i.e. we're fine but have to reroute expresses
                pnode = alt_path[-2]
                nnode = alt_path[-1]
                prior_express = nnode.prev_station
                expresses = [svc for svc in prior_express.station_services if svc.next_station == next_station]

                # previous and next are reversed, since we're going backwards
                new_path = reversed(alt_path[:-2])
                for ss in expresses:
                    prior_express.station_services.remove(ss)
                    prior_express.station_services.append(
                        StationService(
                            pnode.prev_station, pnode.prev_direction,
                            ss.prev_station, ss.prev_direction,
                            ss.route, ss.subroute, True
                        )
                    )
                    for node in new_path:
                        node.station.station_services.append(
                            StationService(
                                node.prev_station, node.prev_direction,
                                node.next_station, node.next_direction,
                                ss.route, ss.subroute, False
                            )
                        )
                    for service in next_station.station_services:
                        if service.route == ss.route and service.subroute == ss.subroute:
                            next_station.station_services.remove(service)
                            next_station.station_services.append(
                                StationService(
                                    service.next_station, service.next_direction,
                                    self, -next_direction,
                                    service.route, service.subroute, True
                                )
                            )

        self.station_services.append(
            StationService(
                next_station, next_direction,
                prev_station, prev_direction,
                route, subroute, True
            )
        )
        return self, (-next_direction if next_direction else None)

StationService = namedtuple('StationService', 'next_station,next_direction,prev_station,prev_direction,route,subroute,stops_here')

def build_graph(dao):
    logging.info('Building graph')
    subroutes = set()
    stations = {}
    session = dao.session()
    for route in dao.routes(): #fltr=orm.Route.route_id.ilike('7%')):
        logging.info('Compiling route %s', route.route_id)
        trips = session.query(orm.Trip).filter(
            orm.Trip.service_id.ilike('%weekday%'),
            orm.Trip.route_id == route.route_id,
            orm.Trip.direction_id == 0
        ).all()
        for trip in trips:
            stop_times = trip.stop_times
            first = stop_times[0].stop_id
            last = stop_times[-1].stop_id
            subroute_name = f'{route.route_id}-{first}-{last}'
            if subroute_name in subroutes:
                continue
            subroutes.add(subroute_name)

            stops, lookahead = tee((st.stop for st in stop_times))
            next(lookahead)
            prev_station = None
            prev_direction = None

            for stop in stops:
                station = stations.get(stop.stop_id)
                if not station:
                    station = stations[stop.stop_id] = Station(stop)

                next_stop = next(lookahead, None)
                if next_stop:
                    next_station = stations.get(next_stop.stop_id)
                    if not next_station:
                        next_station = stations[next_stop.stop_id] = Station(next_stop)

                prev_station, prev_direction = station.add_service(route, subroute_name, prev_station, prev_direction, next_station)
    return stations


def _shift(coords, disp):
    return coords[0] + disp[0], coords[1] + disp[1]


def draw(stations):
    dwg = svgwrite.Drawing('test.svg')
    stations = set(stations.values())
    while stations:
        print('iter')
        traverse(dwg, stations)
    dwg.save()


def traverse(dwg, stations):
    from collections import deque
    start = stations.pop()
    start.map_coords = Point(0, 0)
    queue = deque([start])

    while queue:
        station = queue.popleft()
        if station not in stations:
            if station is not start:
                continue
        else:
            stations.remove(station)
        text = dwg.text(station._gtfs_stop.stop_name, station.map_coords, font_size=1)
        text.rotate(45, station.map_coords)
        dwg.add(text)

        def group(services):
            groups = defaultdict(list)
            for ss in services:
                if ss.next_station:
                    groups[ss.next_direction].append((ss.next_station, ss))
                if ss.prev_station:
                    groups[ss.prev_direction].append((ss.prev_station, ss))
            return groups

        for direction, services in group(station.station_services).items():
            seen_colors = set()
            disp = Point(0, 0)
            for adj_station, service in services:
                color = service.route.route_color or '999999'
                if color in seen_colors:
                    continue
                seen_colors.add(color)
                width = 1
                start = station.map_coords
                if not adj_station.map_coords:
                    adj_station.map_coords = start + Point(direction.x * GRID, direction.y * GRID)
                    queue.append(adj_station)
                end = adj_station.map_coords

                dwg.add(dwg.line(
                    start + disp, end + disp,
                    stroke_width=1, stroke='#'+color
                ))

                dx = end[0] - start[0]
                dy = end[1] - start[1]
                if dy:
                    a = math.atan2(dy, dx)
                    aP = a + 0.5 * math.pi
                    x_disp = width * math.cos(aP)
                    y_disp = width * math.sin(aP)
                    disp += Point(x_disp, y_disp)
                else:
                    disp += Point(width/2, 0)



def main(dao=None):
    logging.basicConfig(level=logging.INFO)
    dao = dao or load()
    stations = build_graph(dao)
    draw(stations)

if __name__ == '__main__':
    main()
