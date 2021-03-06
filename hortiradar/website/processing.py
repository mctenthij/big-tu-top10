import pickle
import random
import urllib.parse
from collections import Counter
from datetime import datetime, timedelta
from hashlib import md5
from itertools import islice
from types import FunctionType
from typing import Sequence

import numpy as np
import peakutils
import requests
import ujson as json
from celery import Celery
from flask import redirect
from redis import StrictRedis
from pattern.nl import sentiment

from hortiradar import Tweety, TOKEN, time_format
from hortiradar.clustering import Token
from hortiradar.database import stop_words, obscene_words, blacklist, get_db
from utils import floor_time

db = get_db()
storiesdb = db.stories
newsdb = db.news

broker_url = "amqp://guest@localhost:5672/hortiradar"
app = Celery("tasks", broker=broker_url)
app.conf.update(task_ignore_result=True, worker_prefetch_multiplier=2)

tweety = Tweety("http://127.0.0.1:8888", TOKEN)
redis = StrictRedis()

CACHE_TIME = 60 * 60


def get_cache_key(func, *args, **kwargs):
    sort_dict = lambda d: sorted(d.items(), key=lambda x: x[0])
    arguments = []
    for a in args:
        if isinstance(a, dict):
            arguments.append(sort_dict(a))
        else:
            arguments.append(a)
    k = (
        func.__name__,
        str(arguments),
        str(sort_dict(kwargs))
    )
    return json.dumps("cache:" + ":".join(k))

# tweety methods return json string
# internal app functions return python dicts/lists
def cache(func, *args, cache_time=CACHE_TIME, force_refresh=False, path="", **kwargs):
    loading_cache_time = 60 * 10
    key = get_cache_key(func, *args, **kwargs)
    v = redis.get(key)

    if v is not None and not force_refresh:
        return json.loads(v) if type(v) == bytes else v
    else:
        loading_id = "loading:" + md5(key.encode("utf-8")).hexdigest()
        if not force_refresh:
            loading = redis.get(loading_id)
            if not loading:
                redis.set(loading_id, b"loading", ex=loading_cache_time)
                cache_request.apply_async((func.__name__, args, kwargs, cache_time, key, loading_id), queue="web")
            return redirect("/hortiradar/loading/{}?redirect={}".format(loading_id.split(":", 1)[1], urllib.parse.quote(path)))
        else:
            redis.set(loading_id, b"loading", ex=loading_cache_time)
            response = func(*args, force_refresh=force_refresh, cache_time=cache_time, **kwargs)
            v = json.dumps(response) if type(response) != bytes else response
            redis.set(key, v, ex=cache_time)
            redis.set(loading_id, b"done", ex=loading_cache_time)
            return response if type(response) != bytes else json.loads(response)

@app.task
def cache_request(func, args, kwargs, cache_time, key, loading_id):
    fun = cache_request.funs[func]
    if fun in [process_top, process_details]:
        kwargs["force_refresh"] = True
    response = fun(*args, cache_time=cache_time, **kwargs)
    v = json.dumps(response) if type(response) != bytes else response
    redis.set(key, v, ex=cache_time)
    redis.set(loading_id, b"done", ex=cache_time)

@app.task(name="tasks.mark_as_spam")
def mark_as_spam(ids: Sequence[str]):
    for id_str in ids:
        tweety.patch_tweet(id_str, data=json.dumps({"spam": 0.8}))

def get_nsfw_prob(image_url: str):
    cache_time = 12 * 60**2
    key = "nsfw:%s" % image_url
    v = redis.get(key)
    if v is not None:
        redis.expire(key, cache_time)
        if v in (b"404", b"415"):
            return 0, int(v)
        else:
            return float(v), 200

    r = requests.post("http://localhost:6000", data={"url": image_url})
    if r.status_code == 200:
        redis.set(key, r.content, ex=cache_time)
        return float(r.content), r.status_code
    elif r.status_code in (404, 415):
        # 415: invalid image
        redis.set(key, str(r.status_code).encode("utf-8"), ex=cache_time)
        return 0, r.status_code
    else:
        return 0, r.status_code


def get_process_top_params(group):
    end = floor_time(datetime.utcnow(), hour=True)
    start = end + timedelta(days=-1)
    params = {
        "start": start.strftime(time_format), "end": end.strftime(time_format),
        "group": group
    }
    return params

def process_top(group, max_amount, params, force_refresh=False, cache_time=CACHE_TIME):
    counts = cache(tweety.get_keywords, force_refresh=force_refresh, cache_time=cache_time, **params)
    total = sum([entry["count"] for entry in counts])

    topkArray = []
    for entry in counts:
        if len(topkArray) < max_amount:
            if entry["keyword"] not in blacklist:
                topkArray.append({"label": entry["keyword"], "y": entry["count"] / total})
        else:
            break

    return topkArray

def process_tokens(prod, params, force_refresh=False, cache_time=CACHE_TIME):
    tweets = cache(tweety.get_keyword, prod, force_refresh=force_refresh, cache_time=CACHE_TIME, **params)

    token_dict = Counter()

    for i in range(len(tweets)):
        tw = tweets[i]
        tokens = [Token(t) for t in tw["tokens"]]

        token_dict.update(tokens)

    occurrences = []
    for (token, count) in token_dict.most_common():
        if not token.filter_token():
            occurrences.append({"text": token.lemma, "pos": token.pos, "weight": count})

    data = {
        "occurrences": occurrences
    }
    return data


def process_details(prod, params, force_refresh=False, cache_time=CACHE_TIME):
    tweets = cache(tweety.get_keyword, prod, force_refresh=force_refresh, cache_time=CACHE_TIME, **params)

    tweetList = []
    unique_tweets = {}
    interaction_tweets = []
    retweets = {}
    imagesList = []
    URLList = []
    word_cloud_dict = Counter()
    tsDict = Counter()
    mapLocations = []
    spam_list = []
    image_tweet_id = {}
    nodes = {}
    edges = []

    for i in range(len(tweets)):
        tw = tweets[i]
        tweet = tw["tweet"]
        lemmas = [t["lemma"] for t in tw["tokens"]]
        texts = [t["text"].lower() for t in tw["tokens"]]  # unlemmatized words
        words = list(set(lemmas + texts))                  # to check for obscene words

        dt = datetime.strptime(tweet["created_at"], "%a %b %d %H:%M:%S +0000 %Y")
        tsDict.update([(dt.year, dt.month, dt.day, dt.hour)])
        tweets[i]["tweet"]["datetime"] = datetime(dt.year, dt.month, dt.day, dt.hour)  # round to hour for peak detection

        # check for spam
        if any(obscene_words.get(t) for t in words):
            spam_list.append(tweet["id_str"])
            continue

        tweetList.append(tweet["id_str"])
        word_cloud_dict.update(lemmas)

        text = " ".join(texts)
        if text not in unique_tweets:
            unique_tweets[text] = tweet["id_str"]

        # track retweets and their retweet counts
        if "retweeted_status" in tweet:
            rt = tweet["retweeted_status"]
            id_str = rt["id_str"]
            retweet_count = rt["retweet_count"]
            if id_str not in retweets or retweet_count > retweets[id_str]:
                retweets[id_str] = retweet_count

        user_id_str = tweet["user"]["id_str"]
        if "retweeted_status" in tweet:
            rt_user_id_str = tweet["retweeted_status"]["user"]["id_str"]

            if rt_user_id_str not in nodes:
                nodes[rt_user_id_str] = tweet["retweeted_status"]["user"]["screen_name"]
            if user_id_str not in nodes:
                nodes[user_id_str] = tweet["user"]["screen_name"]

            edges.append({"source": rt_user_id_str, "target": user_id_str, "value": "retweet"})

        if "user_mentions" in tweet["entities"]:
            if tweet["entities"]["user_mentions"]:
                interaction_tweets.append(tweet["id_str"])

            for obj in tweet["entities"]["user_mentions"]:
                if obj["id_str"] not in nodes:
                    nodes[obj["id_str"]] = obj["screen_name"]
                if user_id_str not in nodes:
                    nodes[user_id_str] = tweet["user"]["screen_name"]

                edges.append({"source": user_id_str, "target": obj["id_str"], "value": "mention"})

        if tweet["in_reply_to_user_id_str"]:
            interaction_tweets.append(tweet["id_str"])

            if tweet["in_reply_to_user_id_str"] not in nodes:
                nodes[tweet["in_reply_to_user_id_str"]] = tweet["in_reply_to_screen_name"]
            if user_id_str not in nodes:
                nodes[user_id_str] = tweet["user"]["screen_name"]

            edges.append({"source": user_id_str, "target": tweet["in_reply_to_user_id_str"], "value": "reply"})

        try:
            for obj in tweet["entities"]["media"]:
                image_url = obj["media_url_https"]
                image_tweet_id[image_url] = tweet["id_str"]
                imagesList.append(image_url)
        except KeyError:
            pass

        try:
            for obj in tweet["entities"]["urls"]:
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

    mark_as_spam.apply_async((spam_list,), queue="web")

    def is_stop_word(token):
        t = token.lower()
        return (len(t) <= 1) or (t.startswith("https://") or t.startswith("http://")) or (t in stop_words)

    word_cloud = []
    for (token, count) in word_cloud_dict.most_common():
        if not is_stop_word(token):
            word_cloud.append({"text": token, "count": count})

    # sentiment analysis on wordcloud
    polarity, subjectivity = sentiment(" ".join(word_cloud_dict.elements()))

    ts = []
    try:
        tsStart = sorted(tsDict)[0]
        tsEnd = sorted(tsDict)[-1]
        temp = datetime(tsStart[0], tsStart[1], tsStart[2], tsStart[3], 0, 0)
        while temp <= datetime(tsEnd[0], tsEnd[1], tsEnd[2], tsEnd[3], 0, 0):
            if (temp.year, temp.month, temp.day, temp.hour) in tsDict:
                ts.append({"year": temp.year, "month": temp.month, "day": temp.day, "hour": temp.hour, "count": tsDict[(temp.year, temp.month, temp.day, temp.hour)]})
            else:
                ts.append({"year": temp.year, "month": temp.month, "day": temp.day, "hour": temp.hour, "count": 0})

            temp += timedelta(hours=1)
    except IndexError:          # when there are 0 tweets
        pass

    # peak detection on time series
    y = np.array([t["count"] for t in ts])
    peaks = peakutils.indexes(y, thres=0.6, min_dist=1).tolist()  # returns a list with the indexes of the peaks in ts

    # peak explanation: the most used words in tweets in the peak
    # the peak indices are sorted in ascending order
    if peaks:
        peak_index = 0
        new_peak = True
        peak_data = {}
        for tw in tweets:
            tweet = tw["tweet"]
            if new_peak:
                p = ts[peaks[peak_index]]
                dt = datetime(p["year"], p["month"], p["day"], p["hour"])
                peak_data[peak_index] = Counter()
                new_peak = False
            if tweet["datetime"] < dt:
                continue
            elif tweet["datetime"] == dt:
                lemmas = [token["lemma"] for token in tw["tokens"]]
                peak_data[peak_index].update(lemmas)
            else:
                new_peak = True
                peak_index += 1
                if peak_index == len(peaks):
                    break

        peaks = [(p, ", ".join(islice(filter(lambda x: not is_stop_word(x), map(lambda x: x[0], peak_data[i].most_common())), 7))) for (i, p) in enumerate(peaks)]

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
    nsfw_list = []
    for (url, count) in Counter(imagesList).most_common():
        if len(images) >= 16:
            break
        nsfw_prob, status = get_nsfw_prob(url)
        if status == 200 and nsfw_prob > 0.8:
            nsfw_list.append(image_tweet_id[url])
        elif status == 200:
            images.append({"link": url, "occ": count})
    mark_as_spam.apply_async((nsfw_list,), queue="web")

    urls = []
    for (url, count) in Counter(URLList).most_common():
        urls.append({"link": url, "occ": count})

    # limit number of nodes/edges
    edges = random.sample(edges, min(len(edges), 250))
    connected_nodes = set([e["source"] for e in edges] + [e["target"] for e in edges])

    graph = {"nodes": [], "edges": []}
    for node in connected_nodes:
        graph["nodes"].append({"id": nodes[node]})

    for edge in edges:
        source = edge["source"]
        target = edge["target"]
        graph["edges"].append({"source": nodes[source], "target": nodes[target], "value": edge["value"]})

    unique_ids = list(unique_tweets.values())

    # retweet ids sorted from most to least tweeted
    if retweets:
        retweet_ids, _ = zip(*sorted(filter(lambda x: x[1] > 0, retweets.items()), key=lambda x: x[1], reverse=True))
    else:
        retweet_ids = []

    start = datetime.strptime(params["start"], time_format)
    end = datetime.strptime(params["end"], time_format)

    items = newsdb.find({"keywords": prod, "pubdate": {"$gte": start, "$lt": end}},
                        projection={"title": True, "pubdate": True, "description": True, "flag": True,
                                    "source": True, "link": True, "nid": True, "_id": False})
    news = sorted([it for it in items], key=lambda x: x["pubdate"], reverse=True)

    data = {
        "tweets": unique_ids,
        "retweets": retweet_ids,
        "interaction_tweets": interaction_tweets,
        "num_tweets": len(tweetList),
        "timeSeries": ts,
        "peaks": peaks,
        "URLs": urls,
        "photos": images,
        "tagCloud": word_cloud,
        "locations": mapLocations,
        "centerloc": avLoc,
        "graph": graph,
        "news": news,
        "polarity": polarity
    }
    return data

def process_stories(group, params, force_refresh=False, cache_time=CACHE_TIME):
    """Load active stories from redis and closed stories from DB.
    Since active stories are story objects, they are processed to JSON from here for rendering in the website"""

    active = redis.get("s:{gr}".format(gr=group))
    if active:
        act = pickle.loads(active)
        active_out = [s.get_jsondict() for s in act]
    else:
        active_out = []

    start = datetime.strptime(params["start"], time_format)
    end = datetime.strptime(params["end"], time_format)

    closed = storiesdb.find({"groups": group, "datetime": {"$gte": start, "$lt": end}},
                            projection={"_id": False})
    if closed:
        closed_out = [s for s in closed]
    else:
        closed_out = []

    sorted_active = sorted(active_out, key=lambda x: len(x["tweets"]), reverse=True)
    sorted_closed = sorted(closed_out, key=lambda x: len(x["tweets"]), reverse=True)

    return sorted_active, sorted_closed

def process_news(keyword, start, end, force_refresh=False, cache_time=CACHE_TIME):
    """Load news messages that are tagged with keyword from DB. The news items are returned in anti-choronological order"""
    if type(start) == str:
        start = datetime.strptime(start, "%Y-%m-%dT%H:%M:%S")  # caching encodes datetimes
    if type(end) == str:
        end = datetime.strptime(end, "%Y-%m-%dT%H:%M:%S")  # caching encodes datetimes

    items = newsdb.find({"keywords": keyword, "pubdate": {"$gte": start, "$lt": end}},
                        projection={"title": True, "pubdate": True, "description": True, "flag": True,
                                    "source": True, "link": True, "nid": True, "_id": False})
    news = sorted([it for it in items], key=lambda x: x["pubdate"], reverse=True)
    return news


funs = {
    "process_details": process_details,
    "process_top": process_top,
    "process_stories": process_stories,
    "process_tokens": process_tokens,
    "process_news": process_news,
}
for f in dir(tweety):
    attr = eval("tweety.{}".format(f))
    if isinstance(attr, FunctionType):
        funs[f] = attr
cache_request.funs = funs
