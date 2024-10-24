import json
import os
import sys
from datetime import datetime, timezone
from influxdb_client import Point, WriteOptions
import influxdb_client
import influxdb_client.client
import influxdb_client.client.write
import pymysql

if not sys.version_info >= (3, 6):
    print("Python version to old!")
    print(sys.version)
    sys.exit(1)

# Load DB Settings
database_file = os.path.join(os.path.dirname(
    os.path.realpath(__file__)), "database.json")
if not os.path.exists(database_file):
    print("Please rename database.json.example to database.json")
    sys.exit(1)

f = open(database_file, 'r')
db = f.read()
f.close()

try:
    db = json.loads(db)
except json.decoder.JSONDecodeError as ex:
    print(database_file + "Json is not valid!")
    print(ex)
    sys.exit(1)
except Exception as ex:
    print("Unhandeld Exception")
    print(ex)
    sys.exit(1)

try:
    MYSQL_CONNECTION = pymysql.connect(host=db['MySQL']['host'],
                                       port=db['MySQL']['port'],
                                       user=db['MySQL']['user'],
                                       password=db['MySQL']['password'],
                                       db=db['MySQL']['database'])
except pymysql.OperationalError as error:
    print(error)
    sys.exit(1)
except Exception as ex:
    print("MySQL connection error")
    print(ex)
    sys.exit(1)

client = influxdb_client.InfluxDBClient(
   url=db["InfluxDB"]["url"],
   token=db["InfluxDB"]["token"],
   org=db["InfluxDB"]["org"]
)

# Select datapoints
if len(sys.argv) > 1 and sys.argv[1].upper().strip() == "ALL":
    MIGRATE_DATAPOINT = ""
    print("Migrate ALL datapoints ...")
elif len(sys.argv) == 2:
    MIGRATE_DATAPOINT = " AND name LIKE '" + sys.argv[1] + "' "
    print("Migrate '" + sys.argv[1] + "' datapoint(s) ...")
else:
    print("To migrate all datapoints run '" + sys.argv[0] + " ALL'")
    print("To migrate one datapoints run '" + sys.argv[0] + " <DATAPONTNAME>'")
    print("To migrate a set of datapoints run '" +
          sys.argv[0] + ' "hm-rega.0.%"' + "'")
    sys.exit(1)
print("")

# dictates how columns will be mapped to key/fields in InfluxDB
SCHEMA = {
    "time_column": "time",  # the column that will be used as the time stamp in influx
    # columns that will map to fields
    "columns_to_fields": ["ack", "q", "from", "value"],
    # "columns_to_tags" : ["",...], # columns that will map to tags
    # table name that will be mapped to measurement
    "table_name_to_measurement": "name",
}

DATATYPES = ["float", "string", "boolean"]

def  is_number_tryexcept(s):
    """ Returns True if string is a number. """
    try:
        float(s)
        return True
    except ValueError:
        return False


#####
# Generates an collection of influxdb points from the given SQL records
#####
def generate_influx_points(datatype, records):
    influx_points = []
    for record in records:
        #tags = {},
        fields = {}
        # for tag_label in SCHEMA['columns_to_tags']:
        #   tags[tag_label] = record[tag_label]
        for field_label in SCHEMA['columns_to_fields']:
            if db['InfluxDB']['store_ack_boolean'] == True:
                if field_label == "ack":
                    if (record[field_label] == 1 or record[field_label] == "True" or record[field_label] == True):
                        record[field_label] = True
                    else:
                        record[field_label] = False

            fields[field_label] = record[field_label]
            
            # Daten in richtigen Typ wandeln
            if field_label == "value":
                if datatype == 0: # ts_number
                    if is_number_tryexcept(record["value"]):
                      fields["value"] = float(record["value"])
                    else:
                      fields["value"] = ""
                elif datatype == 1: # ts_string
                    fields["value"] = str(record["value"])
                elif datatype == 2: # ts_bool
                    fields["value"] = bool(record["value"])
        #influx_points.append
        if fields["value"] != "":
          influx_points.append(Point(record[SCHEMA['table_name_to_measurement']])
                              .time(record[SCHEMA['time_column']])
                              .field(field="ack", value=record["ack"])
                              .field(field="from", value=record["from"])
                              .field(field="q", value=record["q"])
                              .field(field="value", value=record["value"]))

    return influx_points


def query_metrics(table):
    MYSQL_CURSOR.execute(
        "SELECT name, id, type FROM datapoints WHERE id IN(SELECT DISTINCT id FROM " + table + ")" + MIGRATE_DATAPOINT)
    rows = MYSQL_CURSOR.fetchall()
    print('Total metrics in ' + table + ": " + str(MYSQL_CURSOR.rowcount))
    return rows


def migrate_datapoints(table):
    query_max_rows = 100000  # prevent run out of mermory limit on SQL DB
    process_max_rows = 1000

    migrated_datapoints = 0
    metrics = query_metrics(table)
    metric_nr = 0
    metric_count = str(len(metrics))
    processed_rows = 0
    for metric in metrics:
        metric_nr += 1
        if db["InfluxDB"]["org"] == True:
            delete_api = client.delete_api()
            start = datetime(1970, 1, 1, tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            end = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            predicate = '_measurement=' + '"' + metric["name"] + '"'

            delete_api.delete(start, end, predicate=predicate, org="Home", bucket="iobroker")
        print(metric['name'] + "(ID: " + str(metric['id']) + ", type: " + DATATYPES[metric['type']] + ")" +
              " (" + str(metric_nr) + "/" + str(metric_count) + ")")

        start_row = 0
        processed_rows = 0
        while True:
            query = """SELECT d.name,
                                m.ack AS 'ack',
                                (m.q*1.0) AS 'q',
                                s.name AS "from",
                                m.val AS 'value',
                                (m.ts*1000000) AS'time'
                                FROM """ + table + """ AS m
                                LEFT JOIN datapoints AS d ON m.id=d.id
                                LEFT JOIN sources AS s ON m._from=s.id
                                WHERE q=0 AND d.id = """ + str(metric['id']) + """ AND m.val IS NOT NULL
                                ORDER BY m.ts desc
                                LIMIT """ + str(start_row) + """, """ + str(query_max_rows)
            MYSQL_CURSOR.execute(query)
            if MYSQL_CURSOR.rowcount == 0:
                break

            # process x records at a time
            while True:
                selected_rows = MYSQL_CURSOR.fetchmany(process_max_rows)
                if len(selected_rows) == 0:
                    break

                print(f"Processing row {processed_rows + 1:,} to {processed_rows + len(selected_rows):,} from LIMIT {start_row:,} / {start_row + query_max_rows:,} " +
                      table + " - " + metric['name'] + " (" + str(metric_nr) + "/" + str(metric_count) + ")")
                migrated_datapoints += len(selected_rows)
                ip = generate_influx_points(metric['type'], selected_rows)
                write_api = client.write_api(write_options=WriteOptions(batch_size=50_000, flush_interval=10_000))
                try:
                    write_api.write("iobroker", "Home", ip)
                    write_api.close()
                except Exception as ex:
                    print("InfluxDB error")
                    print(ex)
                    sys.exit(1)

                processed_rows += len(selected_rows)

            start_row += query_max_rows
        print("")

    return migrated_datapoints


MYSQL_CURSOR = MYSQL_CONNECTION.cursor(cursor=pymysql.cursors.DictCursor)
migrated = 0
migrated += migrate_datapoints("ts_number")
migrated += migrate_datapoints("ts_bool")
migrated += migrate_datapoints("ts_string")
print(f"Migrated: {migrated:,}")


MYSQL_CONNECTION.close()