import argparse
import configparser
import datetime
import json
import sys
from concurrent.futures import ProcessPoolExecutor
from urllib.parse import quote

import pandas as pd
import requests
from requests_futures.sessions import FuturesSession
from tqdm import tqdm

from facebook import GraphAPI, GraphAPIError

# Facebook


def fb_query(url):
    og_object = None
    og_engagement = None
    og_error = None

    try:
        fb_response = fb_graph.get_object(
            id=url,
            fields="engagement, og_object"
        )

        if 'og_object' in fb_response:
            og_object = fb_response['og_object']
        if 'engagement' in fb_response:
            og_engagement = fb_response['engagement']
    except Exception as e:
        og_error = e

    return (og_object, og_engagement, og_error)

# Facebook


def fb_queries(urls):
    results = {}

    try:
        responses = fb_graph.get_objects(
            ids=[url.strip() for url in urls],
            fields="engagement, og_object"
        )
    except:
        raise

    for url, r in responses.items():
        og_object = None
        og_engagement = None
        og_error = None

        if 'og_object' in r:
            og_object = r['og_object']
        if 'engagement' in r:
            og_engagement = r['engagement']
        results[url] = (og_object, og_engagement, og_error)

    return results


def get_app_access(app_id, app_secret, version="2.10"):
    """Exchange a short-lived user token for a long-lived one"""
    payload = {'grant_type': 'client_credentials',
               'client_id': app_id,
               'client_secret': app_secret}

    try:
        response = requests.post(
            'https://graph.facebook.com/oauth/access_token?', params=payload)
    except requests.exceptions.RequestException:
        raise Exception()

    token = json.loads(response.text)
    token['created'] = str(datetime.datetime.now())
    return token


def extend_user_access(user_token, app_id, app_secret, version="2.10"):
    """Uses a short-lived user token to create a long lived one"""
    payload = {'grant_type': 'fb_exchange_token',
               'client_id': app_id,
               'client_secret': app_secret,
               'fb_exchange_token': user_token}

    try:
        response = requests.post(
            'https://graph.facebook.com/oauth/access_token?', params=payload)
    except requests.exceptions.RequestException:
        raise Exception()

    token = json.loads(response.text)
    token['created'] = str(datetime.datetime.now())
    return token


def token_expiry(token):
    remain = datetime.timedelta(seconds=token['expires_in'])
    created = datetime.datetime.strptime(
        token['created'], "%Y-%m-%d %H:%M:%S.%f")
    print("Token expires {}\n{} left".format(str(created+remain), str(remain)))


def expires_soon(token, tolerance=1):
    remain = datetime.timedelta(seconds=token['expires_in'])
    created = datetime.datetime.strptime(
        token['created'], "%Y-%m-%d %H:%M:%S.%f")
    now = datetime.datetime.now()

    if (now - created+remain).days < tolerance:
        return True
    else:
        return False


def init_config(config_file):
    # Load config
    Config = configparser.ConfigParser()
    Config.read(config_file)

    FACEBOOK_APP_ID = Config.get('facebook', 'app_id')
    FACEBOOK_APP_SECRET = Config.get('facebook', 'app_secret')
    FACEBOOK_USER_TOKEN = Config.get('facebook', 'user_token')

    try:
        with open("token.pkl", "rb") as pkl:
            token = pickle.load(pkl)
            print("Found pickled token")

        if expires_soon(token):
            token = extend_user_access(
                FACEBOOK_USER_TOKEN, FACEBOOK_APP_ID, FACEBOOK_APP_SECRET)
            print("Created new token, because of soon expiry")
    except FileNotFoundError:
        print("No token found. Creating new one...")
        token = extend_user_access(
            FACEBOOK_USER_TOKEN, FACEBOOK_APP_ID, FACEBOOK_APP_SECRET)

    print("Saving token")
    token_expiry(token)
    with open("token.pkl", "wb") as pkl:
        pickle.dump(token, pkl)

    return GraphAPI(token['access_token'], version="2.10")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description='Query FB URL with DOI-URLs')
    parser.add_argument('-i', '--input', required=True,
                        help='Specifiy input file')
    parser.add_argument('-p', '--parallel', default=50, type=int,
                        help='Number of parallel requests (default/max=50)')
    parser.add_argument('-d', '--DOI-only', dest='doi-only', action='store_true',
                        help='Don\'t query URLs based on DOI resolving')
    args = vars(parser.parse_args())

    # Init Facebook
    fb_graph = init_config("config.cnf")

    # Init dataset
    df = pd.read_csv(args['input'])
    df = df[df.url.notnull()]  # Require a URL

    # Create alternative URLs
    print("All URLs are http or https: {}".format(
        len(df) == df.url.map(lambda x: x[:4] == "http").sum()
    ))

    if not args['doi-only']:
        urls = ['url1', 'url2', 'url3', 'url4']
        df['url1'] = df.url
        df['url2'] = df.url.map(lambda x: x[:4] + x[5:]
                                if x[4] == "s" else x[:4] + "s" + x[4:])
        df['url3'] = df.doi.map(lambda x: "https://doi.org/{}".format(x))
        df['url4'] = df.doi.map(lambda x: "http://dx.doi.org/{}".format(x))
    else:
        urls = ['url1', 'url2']
        df['url1'] = df.doi.map(lambda x: "https://doi.org/{}".format(x))
        df['url2'] = df.doi.map(lambda x: "http://dx.doi.org/{}".format(x))

    # Create temp out
    out_df = df[urls+['doi']].copy()

    res_cols = []
    for i in range(1, len(urls)+1):
        res_cols.append("og_obj"+str(i))
        res_cols.append("og_eng"+str(i))
        res_cols.append("og_err"+str(i))
    res_cols.append("ts")

    for col in res_cols:
        out_df[col] = None

    batchsize = args['parallel']

    failed_ind = set()
    start_inds = list(range(0, len(out_df), batchsize))
    with open(args['input'].split(".csv")[0] + "_fb.csv", "w") as outfile:
        out_df.iloc[[]].to_csv(outfile, index=False)
        for i in tqdm(start_inds, desc="Collecting in batches"):
            curr_ind = list(range(i, i+batchsize))
            batch = out_df.iloc[curr_ind].copy()

            # Query and process fb_queries for original URL
            try:
                for ix, col in enumerate(urls, 1):

                    now = datetime.datetime.now()
                    us = [x for x in batch[col].tolist() if pd.notnull(x)]
                    results = fb_queries(us)

                    for url, resp in results.items():
                        if resp[0]:
                            batch.loc[batch[col] == url,
                                      'og_obj'+str(ix)] = json.dumps(resp[0])
                        if resp[1]:
                            batch.loc[batch[col] == url,
                                      'og_eng'+str(ix)] = json.dumps(resp[1])
                        if resp[2]:
                            batch.loc[batch[col] == url,
                                      'og_err'+str(ix)] = str(resp[2])
                        batch.loc[batch[col] == url, 'ts'] = str(now)
                batch.to_csv(outfile, index=False, header=False)
            except Exception as e:
                print(e)
                for i in curr_ind:
                    failed_ind.add(i)

        # Individually re-query failed rows
        rows = list(out_df.iloc[list(failed_ind)].iterrows())
        for i, row in tqdm(rows, desc="Collecting failed rows"):
            for ix, col in enumerate(urls, 1):
                resp = fb_query(row[col])
                if resp[0]:
                    out_df.iloc[0, 'og_obj'+str(ix)] = json.dumps(resp[0])
                if resp[1]:
                    out_df.iloc[0, 'og_eng'+str(ix)] = json.dumps(resp[1])
                if resp[2]:
                    out_df.iloc[0, 'og_err'+str(ix)] = str(resp[2])
                out_df.loc[i, 'ts'] = str(now)

            out_df.iloc[0].to_csv(outfile, index=False, header=False)
