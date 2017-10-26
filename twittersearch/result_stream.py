# This Python file uses the following encoding: utf-8

import time
import re
import logging
import requests
try:
    import ujson as json
except ImportError:
    import json
from tweet_parser.tweet import Tweet

from .utils import *
from .api_utils import *

logger = logging.getLogger(__name__)

def make_session(username=None, password=None, bearer_token=None):
    """Creates a Requests Session for use. Accepts a bearer token 
    for freemium users and will override username and password information if
    present.

    Args:
        username (str): username for the session
        password (str): password for the user
        bearer_token (str): token for the session for freemium.
    """

    if password is None and bearer_token is None:
        logger.error("No authentication information provided; please check your object")
        raise KeyError

    session = requests.Session()
    headers = {'Accept-encoding': 'gzip'}
    if bearer_token:
        logger.warning("using bearer token for authentication")
        headers['Authorization'] = "Bearer {}".format(bearer_token)
        session.headers = headers
    else:
        logger.warning("using username and password for authentication")
        session.auth = username, password
        session.headers = headers
    return session


def retry(func):
    """
    Decorator to handle API retries and exceptions. Defaults to three retries.

    Args:
        func (function): function for decoration

    Returns:
        decorated function

    """
    def retried_func(*args, **kwargs):
        max_tries = 3
        tries = 0
        while True:
            try:
                resp = func(*args, **kwargs)

            except requests.exceptions.ConnectionError as exc:
                exc.msg = "Connection error for session; exiting"
                raise exc

            except requests.exceptions.HTTPError as exc:
                exc.msg = "HTTP error for session; exiting"
                raise exc

            if resp.status_code != 200 and tries < max_tries:
                logger.warning("retrying request; current status code: {}"
                               .format(resp.status_code))
                tries += 1
                # mini exponential backoff here. 
                time.sleep(tries ** 2)
                continue

            break

        if resp.status_code != 200:
            logger.error("HTTP Error code: {}: {}"
                         .format(resp.status_code,
                                 GNIP_RESP_CODES[str(resp.status_code)]))
            logger.error("rule payload: {}".format(kwargs["rule_payload"]))
            raise requests.exceptions.HTTPError

        return resp

    return retried_func


@retry
def request(session, url, rule_payload, **kwargs):
    """
    Executes a request with the given payload and arguments.

    Args:
        session (requests.Session): the valid session object
        url (str): Valid API endpoint
        rule_payload (str or dict): rule package for the POST. if you pass a
            dictionary, it will be converted into JSON.
    """
    if isinstance(rule_payload, dict):
        rule_payload = json.dumps(rule_payload)
    result = session.post(url, data=rule_payload, **kwargs)
    return result


class ResultStream:
    """Class to represent an API query that handles two major functionality
    pieces: wrapping metadata around a specific API call and automatic
    pagination of results.
    """

    def __init__(self, url, rule_payload, username=None, password=None,
                 bearer_token=None, max_tweets=1000,
                 tweetify=True, max_pages=None, **kwargs):
        """
        Args:
            username (str): username
            password (str): password
            bearer_token (str): bearer token for freemium users
            url (str): API endpoint; should be generated using the
                `gen_endpoint` function.
            rule_payload (json or dict): payload for the post request
            max_tweets (int): max results that will be fetched from the API.
            tweetify (bool): If you are grabbing tweets and not counts, use the
                tweet parser library to convert each raw tweet package to a Tweet
                with lazy properties.

        """

        self.username = username
        self.password = password
        self.bearer_token = bearer_token
        self.url = url
        if isinstance(rule_payload, str):
            rule_payload = json.loads(rule_payload)
        self.rule_payload = rule_payload
        self.tweetify = tweetify
        # magic number of max tweets if you pass a non_int
        self.max_tweets = max_tweets if isinstance(max_tweets, int) else 10 ** 15

        self.total_results = 0
        self.n_requests = 0
        self.session = None
        self.current_tweets = None
        self.next_token = None
        self.stream_started = False
        self._tweet_func = Tweet if tweetify else lambda x: x
        self.max_pages = max_pages if max_pages is not None else 10 ** 9 # magic number of pages!
        validate_count_api(self.rule_payload, self.url)


    def stream(self):
        """
        Main entry point for the data from the API. Will automatically paginate
        through the results via the 'next' token and return up to `max_tweets`
        tweets or up to `max_pages` api calls, whichever is lower.

        Usage:
            >>> result_stream = ResultStream(**kwargs)
            >>> stream = result_stream.stream()
            >>> results = list(stream)
            >>> # or for faster usage...
            >>> results = list(ResultStream(**kwargs).stream())
        """
        self.init_session()
        self.check_counts()
        self.execute_request()
        self.stream_started = True
        while True:
            for tweet in self.current_tweets:
                if self.total_results >= self.max_tweets:
                    break
                yield self._tweet_func(tweet)
                self.total_results += 1

            if self.next_token and self.total_results < self.max_tweets and self.n_requests <= self.max_pages:
                self.rule_payload = merge_dicts(self.rule_payload, ({"next": self.next_token}))
                logger.info("paging; total requests read so far: {}".format(self.n_requests))
                self.execute_request()
            else:
                break
        logger.info("ending stream at {} tweets".format(self.total_results))
        self.current_tweets = None
        self.session.close()

    def init_session(self):
        """
        Defines a session object for passing requests.
        """
        if self.session:
            self.session.close()
        self.session = make_session(self.username, self.password, self.bearer_token)

    def check_counts(self):
        """
        Disables tweet parsing if the count api is used.
        """
        if "counts" in re.split("[/.]", self.url):
            logger.info("disabling tweet parsing due to counts api usage")
            self._tweet_func = lambda x: x

    def execute_request(self):
        """
        Sends the request to the api and parses the json response.

        """
        if self.n_requests % 20 == 0 and self.n_requests > 1:
            logger.info("refreshing session")
            self.init_session()
        resp = request(session=self.session,
                       url=self.url,
                       rule_payload=self.rule_payload,
                      )
        self.n_requests += 1
        resp = json.loads(resp.content.decode(resp.encoding))
        self.next_token = resp.get("next", None)
        self.current_tweets = resp["results"]

    def __repr__(self):
        repr_keys = ["username", "url", "rule_payload", "tweetify", "max_tweets"]
        str_ = json.dumps(dict([(k, self.__dict__.get(k)) for k in repr_keys]), indent=4)
        str_ = "ResultStream: \n\t" + str_
        return str_


def collect_results(rule, max_tweets=500, result_stream_args=None):
    """Utility function to quickly get a list of tweets from a resultstream
    without keeping the object around. Rqequires your args to be configured
    prior to using.
    """
    if result_stream_args is None:
        logger.error("This function requires a configuration dict for the "
                     "inner ResultStream object.")
        raise KeyError

    rs = ResultStream(**result_stream_args,
                      rule_payload=rule,
                      max_tweets=max_tweets)
    return list(rs.stream())
