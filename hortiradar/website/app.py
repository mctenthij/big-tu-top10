from collections import Counter
from datetime import datetime, timedelta
from time import sleep

from flask import Blueprint, Response, render_template, request, render_template_string
from flask_babel import Babel
from flask_mail import Mail
from flask_user import login_required, UserManager, SQLAlchemyAdapter
from redis import StrictRedis
import requests
import ujson as json

from hortiradar import Tweety, TOKEN, time_format
from hortiradar.database import GROUPS
from hortiradar.website import app, db
from models import User


bp = Blueprint("horti", __name__, template_folder="templates", static_folder="static")

# Initialize flask extensions
babel = Babel(app)
mail = Mail(app)

# Setup Flask-User
db_adapter = SQLAlchemyAdapter(db, User)        # Register the User model
user_manager = UserManager(db_adapter, app)     # Initialize Flask-User

tweety = Tweety("http://127.0.0.1:8888", TOKEN)
redis_namespace = ""
redis = StrictRedis()

CACHE_TIME = 60 * 60
groups = sorted(GROUPS.keys())

def get_cache_key(func, *args, **kwargs):
    k = (
        func.__name__,
        str(args),
        str(sorted(kwargs.items(), key=lambda x: x[0]))
    )
    return redis_namespace + json.dumps(":".join(k))


# tweety methods return json string
# internal app functions return python dicts/lists
def cache(func, *args, **kwargs):
    force_refresh = kwargs.pop("force_refresh", None) or False
    cache_time = kwargs.pop("cache_time", None) or CACHE_TIME
    key = get_cache_key(func, *args, **kwargs)
    v = redis.get(key)
    if v == "loading" and not force_refresh:
        sleep(0.7)
        kwargs["force_refresh"] = force_refresh
        kwargs["cache_time"] = cache_time
        return cache(func, *args, **kwargs)
    elif v is not None and not force_refresh:
        return json.loads(v) if type(v) == bytes else v
    else:
        if not force_refresh:
            redis.set(key, "loading", ex=60)
        response = func(*args, force_refresh=force_refresh, cache_time=cache_time, **kwargs)
        v = json.dumps(response) if type(response) != bytes else response
        redis.set(key, v, ex=cache_time)
        return response if type(response) != bytes else json.loads(response)


def jsonify(**kwargs):
    return Response(json.dumps(kwargs), status=200, mimetype="application/json")

def round_time(dt):
    return dt + timedelta(minutes=-dt.minute, seconds=-dt.second, microseconds=-dt.microsecond)

@bp.route("/")
def home():
    sync_time = redis.get(redis_namespace + "sync_time")
    if sync_time:
        sync_time = sync_time.decode("utf-8")
    return render_template("home.html", title=make_title("BigTU research project"), sync_time=sync_time)

@bp.route("/widget/<group>")
def top_widget(group):
    """A small widget showing the top 5 in the group."""
    max_amount = request.args.get("k", 10, type=int)  # this is 10, so we re-use the cached data from the top 10
    data = cache(process_top, group, max_amount)[:5]
    data = [d["label"] for d in data]
    return render_template("widget.html", data=data)

@bp.route("/groups/")
@bp.route("/groups")
def view_groups():
    return render_template("groups.html", title=make_title("Groepen"), groups=groups)

@bp.route("/groups/<group>")
def view_group(group):
    params = get_process_top_params(group)
    keywords = cache(tweety.get_keywords, **params)
    total = sum([entry["count"] for entry in keywords])
    for keyword in keywords:
        keyword["percentage"] = "{:.2f}".format(keyword["count"] / total * 100)
    nums = range(1, len(keywords) + 1)
    template_data = {"nums_keywords": zip(nums, keywords), "group": group, "nums": nums, "total": total}
    return render_template("group.html", title=make_title(group), **template_data)

@bp.route("/details")
def details():
    return render_template("details.html")

@bp.route("/about")
def about():
    stats = json.loads(redis.get("t:stats"))
    return render_template("about.html", title="Over de Hortiradar", **stats)

@app.errorhandler(404)
def page_not_found(error):
    return render_template("page_not_found.html"), 404

@bp.route("/_add_top_k/<group>")
def show_top(group):
    """Visualize a top k result file"""
    max_amount = request.args.get("k", 10, type=int)
    data = cache(process_top, group, max_amount)
    return jsonify(result=data)

@bp.route("/_get_details")
def show_details():
    """
    Visualize the details of a top k product
    product:    Product for which the details page should be constructed
    interval:   Interval in seconds for which tweets should be extracted through API
    """
    prod = request.args.get("product", "", type=str)
    interval = request.args.get("interval", 60 * 60 * 24 * 7, type=int)
    end = request.args.get("end", "", type=str)
    if end:
        end = datetime.strptime(end, "%Y-%m-%d %H:%M") + timedelta(hours=1)
    else:
        end = round_time(datetime.utcnow())
    start = end + timedelta(seconds=-interval)
    params = {"start": start.strftime(time_format), "end": end.strftime(time_format)}
    details = cache(process_details, prod, params)
    return jsonify(result=details)

@bp.route("/_get_clusters")
def show_clusters():
    # TODO: currently loading in file with clusters, implement caching of clusters
    now = round_time(datetime.utcnow())
    cluster_file = now.strftime("%Y%m%d_%H_clusters.json")
    with open(cluster_file) as f:
        clusters = json.load(f)
    return jsonify(clusters)

@bp.route("/members")
@login_required
def members_page():
    return render_template_string("""
    {% extends "base.html" %}
    {% block content %}
        <h2>Members page</h2>
        <p>This page can only be accessed by authenticated users.</p><br/>
        <p><a href={{ url_for('home') }}>Home page</a> (anyone)</p>
        <p><a href={{ url_for('members_page') }}>Members page</a> (login required)</p>
    {% endblock %}
    """)

def get_process_top_params(group):
    end = round_time(datetime.utcnow())
    start = end + timedelta(days=-1)
    params = {
        "start": start.strftime(time_format), "end": end.strftime(time_format),
        "group": group
    }
    return params

def process_top(group, max_amount, force_refresh=False, cache_time=CACHE_TIME):
    params = get_process_top_params(group)
    counts = cache(tweety.get_keywords, force_refresh=force_refresh, cache_time=cache_time, **params)
    total = sum([entry["count"] for entry in counts])

    # tags in the first line are still in flowers.txt, tags from the second line are not
    BLACKLIST = ["fhgt", "fhtf", "fhalv", "fhglazentulp", "fhgt2014", "fhgt2015", "aalsmeer", "westland", "fh2020", "bloemistenklok", "morgenvoordeklok", "fhstf", "floraholland", "fhmagazine", "floranext", "bos",
                 "community", "glastuinbouw", "klok", "komindekas", "tuinbouw", "westland", "aalsmeer", "aanvoertijden", "naaldwijk", "presentatieruimte", "tuincentra", "tuincentrum", "valentijn", "veiling", "viool", "viooltjes"]
    topkArray = []
    for entry in counts:
        if len(topkArray) < max_amount:
            if entry["keyword"] not in BLACKLIST:
                topkArray.append({"label": entry["keyword"], "y": entry["count"] / total})
        else:
            break

    return topkArray

def process_details(prod, params, force_refresh=False, cache_time=CACHE_TIME):
    tweets = cache(tweety.get_keyword, prod, force_refresh=force_refresh, cache_time=CACHE_TIME, **params)

    tweetList = []
    imagesList = []
    URLList = []
    wordCloudDict = Counter()
    tsDict = Counter()
    mapLocations = []

    for tw in tweets:
        tweet = tw["tweet"]
        tweetList.append(tweet["id_str"])

        tokens = [t["lemma"] for t in tw["tokens"]]
        wordCloudDict.update(tokens)

        dt = datetime.strptime(tweet["created_at"], "%a %b %d %H:%M:%S +0000 %Y")
        tsDict.update([(dt.year, dt.month, dt.day, dt.hour)])

        try:
            for obj in tweet["entities"]["media"]:
                imagesList.append(obj["media_url_https"])
        except KeyError:
            pass

        try:
            for obj in tweet["entities"]["urls"]:
                # using "expand" here synchronously will slow everything down tremendously
                url = obj["expanded_url"]
                if url is not None:
                    URLList.append(url)
        except KeyError:
            pass

        try:
            if tweet["coordinates"] is not None:
                if tweet["coordinates"]["type"] == "Point":
                    coords = tweet["coordinates"]["coordinates"]
                    mapLocations.append({"lng": coords[0], "lat": coords[1]})
        except KeyError:
            pass

    wordCloud = []
    for token in wordCloudDict:
        if token.lower() not in stop_words and "http" not in token and len(token) > 1:
            wordCloud.append({"text": token, "weight": wordCloudDict[token]})

    ts = []
    tsStart = sorted(tsDict)[0]
    tsEnd = sorted(tsDict)[-1]
    temp = datetime(tsStart[0], tsStart[1], tsStart[2], tsStart[3], 0, 0)
    while temp < datetime(tsEnd[0], tsEnd[1], tsEnd[2], tsEnd[3], 0, 0):
        if (temp.year, temp.month, temp.day, temp.hour) in tsDict:
            ts.append({"year": temp.year, "month": temp.month, "day": temp.day, "hour": temp.hour, "value": tsDict[(temp.year, temp.month, temp.day, temp.hour)]})
        else:
            ts.append({"year": temp.year, "month": temp.month, "day": temp.day, "hour": temp.hour, "value": 0})

        temp += timedelta(hours=1)

    lng = 0
    lat = 0
    if mapLocations:
        for loc in mapLocations:
            lng += loc["lng"]
            lat += loc["lat"]
            avLoc = {"lng": lng / len(mapLocations), "lat": lat / len(mapLocations)}
    else:
        avLoc = {"lng": 5, "lat": 52}

    images = []
    for (url, count) in Counter(imagesList).most_common():
        images.append({"link": url, "occ": count})

    urls = []
    for (url, count) in Counter(URLList).most_common():
        urls.append({"link": url, "occ": count})

    data = {
        "tweets": tweetList[::-1],
        "timeSeries": ts,
        "URLs": urls,
        "photos": images,
        "tagCloud": wordCloud,
        "locations": mapLocations,
        "centerloc": avLoc
    }
    return data

def expand(url):
    """Expands URLs from URL shorteners."""
    try:
        r = requests.head(url)
        while r.is_redirect and r.headers.get("location") is not None:
            url = r.headers["location"]
            r = requests.head(url)
        return r.url
    except:
        return url

def make_title(page):
    return page + " — Hortiradar"


with open("../database/data/stoplist-nl.txt", "rb") as f:
    stop_words = [w.decode("utf-8").strip() for w in f]
    stop_words = {w: 1 for w in stop_words}  # stop words to filter out in word cloud

app.register_blueprint(bp, url_prefix="/hortiradar")

if __name__ == "__main__":
    app.run(debug=True, port=8000)