from collections import defaultdict, namedtuple
from itertools import tee
import logging

from gtfslib.dao import Dao
from gtfslib import orm
import requests
import svgwrite


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

def _coords(stop):
    # lat is 40.5 - 40.9
    lat = (stop.stop_lat - 40.5) * -1000
    # lon is -74.2 - -73.75
    lon = (stop.stop_lon + 74.5) * 1000
    return lon, lat

class Station:
    def __init__(self, stop):
        self._gtfs_stop = stop
        self.services = {}

    @property
    def coords(self):
        return _coords(self._gtfs_stop)

    def add_service(self, route, subroute, next_stop):
        self.services[(route.route_id, subroute)] = (route, next_stop)



def build_graph(dao):
    logging.info('Building graph')
    subroutes = set()
    stations = {}
    session = dao.session()
    for route in dao.routes():
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
                station.add_service(route, subroute_name, next_stop)
    return stations



def draw(stations):
    dwg = svgwrite.Drawing('test.svg')
    for station in stations.values():
        for route, next_stop in station.services.values():
            color = route.route_color or '999999'
            if next_stop:
                dwg.add(dwg.line(
                    station.coords, _coords(next_stop),
                    stroke_width=1, stroke='#'+color
                ))
    dwg.save()

def main(dao=None):
    logging.basicConfig(level=logging.INFO)
    dao = dao or load()
    draw(build_graph(dao))
    print(min_lat, max_lat, min_lon, max_lon)

if __name__ == '__main__':
    main()
