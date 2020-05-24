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


class Point(namedtuple('Point', 'x,y')):
    def __add__(self, other):
        return Point(self.x + other.x, self.y + other.y)

    def __sub__(self, other):
        return Point(self.x - other.x, self.y - other.y)


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
        self.next_stops = defaultdict(list)

    def __hash__(self):
        return hash(self._gtfs_stop.stop_id)

    def add_service(self, route, subroute, next_station):
        if next_station:
            diff = next_station.coords - self.coords

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

            self.next_stops[direction].append((next_station, route, subroute))


def build_graph(dao):
    logging.info('Building graph')
    subroutes = set()
    stations = {}
    session = dao.session()
    for route in dao.routes(): #fltr=orm.Route.route_id == 'L'):
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

            for stop in stops:
                station = stations.get(stop.stop_id)
                if not station:
                    station = stations[stop.stop_id] = Station(stop)

                next_stop = next(lookahead, None)
                if next_stop:
                    next_station = stations.get(next_stop.stop_id)
                    if not next_station:
                        next_station = stations[next_stop.stop_id] = Station(next_stop)

                station.add_service(route, subroute_name, next_station)
    return stations


def _shift(coords, disp):
    return coords[0] + disp[0], coords[1] + disp[1]


def draw(stations):
    dwg = svgwrite.Drawing('test.svg')
    for station in stations.values():
        if not station.map_coords:
            station.map_coords = Point(0, 0)

        for direction, routes in station.next_stops.items():
            colors = {(route[1].route_color or '999999', route[0]) for route in routes}
            disp = Point(0, 0)
            for color, next_stop in colors:
                width = 1
                start = station.map_coords
                if not next_stop.map_coords:
                    next_stop.map_coords = start + Point(direction.x * GRID, direction.y * GRID)
                end = next_stop.map_coords

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

    dwg.save()


def main(dao=None):
    logging.basicConfig(level=logging.INFO)
    dao = dao or load()
    draw(build_graph(dao))

if __name__ == '__main__':
    main()
