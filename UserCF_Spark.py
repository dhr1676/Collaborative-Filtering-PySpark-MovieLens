# coding=utf-8
"""
Created on 2019-04-16

@author: Haoran Ding
"""
import argparse
import time
from math import sqrt, log
from operator import add

from pyspark import SparkContext

N_SIM_USER = 20
N_REC_MOVIE = 10


def read_data(file_path, sparkContext):
    """
    :param file_path:
    :param sparkContext:
    :return: RDD(userID, movieID)
    """
    data_rdd = sparkContext.textFile(file_path, use_unicode=False) \
        .map(lambda line: line.strip()) \
        .map(lambda line: line.split(",")) \
        .map(lambda line: (int(line[0]), int(line[1])))

    (train_rdd, test_rdd) = data_rdd.randomSplit(weights=[0.75, 0.25], seed=0)
    print "Read data finished!"
    return train_rdd, test_rdd


def calc_user_sim(train_rdd):
    # Get Item-User inverse table
    print 'building movie-users inverse table...'
    movie2users = train_rdd \
        .map(lambda (user, movie): (movie, user)) \
        .groupByKey(numPartitions=40) \
        .map(lambda (movie, user_list): (movie, [u for u in user_list]))

    # Count popularity
    movie_popular = movie2users \
        .map(lambda (movie, user_list): (movie, len(user_list)))

    all_movie_count = movie2users.count()

    # Get user-co-rated matrix C[u][v]
    user_co_rated_matrix = movie2users \
        .map(lambda (movie, user_list): get_user_sim_matrix(movie, user_list)) \
        .flatMap(lambda uv_list: uv_list) \
        .map(lambda (u, v): ((u, v), 1)) \
        .reduceByKey(add, numPartitions=40)
    print 'build movie-users inverse table succ'

    # N[u]
    view_num_map = train_rdd \
        .map(lambda (user, movie): (user, 1)) \
        .reduceByKey(add, numPartitions=40) \
        .collectAsMap()

    # Get user similarity matrix W[u][v]: RDD((u, v), score)
    user_sim_matrix = user_co_rated_matrix \
        .map(lambda ((u, v), count): ((u, v), count / sqrt(view_num_map[u] * view_num_map[v])))
    print 'calculate user similarity matrix(similarity factor) successful'
    return user_sim_matrix, movie_popular, all_movie_count


def get_user_sim_matrix(movie, user_list):
    uv_list = []
    user_list.sort()
    for u in user_list:
        for v in user_list:
            if u == v:
                continue
            uv_list.append((u, v))
    return uv_list


def recommend(user, watched_movies, _user_sim_matrix_map, other_user_history):
    K = N_SIM_USER
    N = N_REC_MOVIE

    rank = {}

    sort_user_list = sorted(_user_sim_matrix_map.items(), key=lambda x: x[1], reverse=True)[:K]
    for similar_user, similarity_factor in sort_user_list:
        for movie in other_user_history[similar_user]:
            if movie in watched_movies:
                continue
            rank.setdefault(movie, 0)
            rank[movie] += similarity_factor

    return sorted(rank.items(), key=lambda x: x[1], reverse=True)[:N]


def evaluate(train_rdd, test_rdd, user_sim_matrix_rdd, all_movie_count, movie_popular):
    N = N_REC_MOVIE

    # dict for popularity
    movie_popular_dict = movie_popular.collectAsMap()

    test_user_movie = test_rdd \
        .groupByKey(numPartitions=40) \
        .map(lambda (user, movie_list): (user, set([m for m in movie_list])))

    test_u_m_map = test_user_movie.collectAsMap()

    user_sim_matrix_map = user_sim_matrix_rdd \
        .map(lambda ((u, v), score): (u, (v, score))) \
        .groupByKey(numPartitions=40) \
        .map(lambda (u, v_set_list): (u, {v_set[0]: v_set[1] for v_set in v_set_list})) \
        .collectAsMap()

    train_user_movie = train_rdd \
        .groupByKey(numPartitions=40) \
        .map(lambda (user, movie_list): (user, [m for m in movie_list]))

    train_user_history = train_user_movie.collectAsMap()

    train_user_list = train_user_movie \
        .map(lambda (user, movie_list):
             (user, recommend(user, movie_list, user_sim_matrix_map[user], train_user_history))) \
        .map(lambda (user, recommend_dict):
             (user, calc_hit(recommend_dict, test_u_m_map.get(user, {}), N, movie_popular_dict))) \
        .persist()

    pre_recall = train_user_list \
        .map(lambda (user, (hit, rec_count, test_count, popular_sum, coverage_set)):
             (hit, rec_count, test_count, popular_sum)) \
        .collect()
    all_rec_count = train_user_list \
        .map(lambda (user, (hit, rec_count, test_count, popular_sum, coverage_set)): coverage_set) \
        .flatMap(lambda movie_set: movie_set) \
        .distinct() \
        .count()

    train_user_list.unpersist()

    _hit, _rec_count, _test_count, _popular_sum = 0, 0, 0, 0

    for i in pre_recall:
        _hit += i[0]
        _rec_count += i[1]
        _test_count += i[2]
        _popular_sum += i[3]

    precision = _hit / (1.0 * _rec_count)
    recall = _hit / (1.0 * _test_count)
    popularity = _popular_sum / (1.0 * _rec_count)
    coverage = all_rec_count / (1.0 * all_movie_count)
    print 'precision=%.4f, recall=%.4f' % (precision, recall)
    print 'coverage = %.4f, popularity = %.4f ' % (coverage, popularity)
    return


def calc_hit(recommend_dict, test_movie_list, N, movie_popular_dict):
    hit = 0
    popular_sum = 0
    coverage_set = set()
    for movie, score in recommend_dict:
        if movie in test_movie_list:
            hit += 1
        popular_sum += log(1 + movie_popular_dict[movie])
        coverage_set.add(movie)
    return hit, N, len(test_movie_list), popular_sum, coverage_set


if __name__ == '__main__':
    start_time = time.time()

    parser = argparse.ArgumentParser(description='UserCF Spark',
                                     formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('--n_sim_movie')
    parser.add_argument('--n_rec_movie')
    parser.add_argument('--input', default=None, help='Input Data')
    parser.add_argument('--master', default="local[20]", help="Spark Master")

    verbosity_group = parser.add_mutually_exclusive_group(required=False)
    verbosity_group.add_argument('--verbose', dest='verbose', action='store_true')
    verbosity_group.add_argument('--silent', dest='verbose', action='store_false')
    parser.set_defaults(verbose=False)

    args = parser.parse_args()
    sc = SparkContext(args.master, 'UserCF Spark Version')

    if not args.verbose:
        sc.setLogLevel("ERROR")

    train_set, test_set = read_data(file_path=args.input, sparkContext=sc)
    user_similarity_matrix, movie_popular_count, movie_total_count = calc_user_sim(train_rdd=train_set)
    evaluate(train_set, test_set, user_similarity_matrix, movie_total_count, movie_popular_count)

    end_time = time.time()
    print "Time elapse: %.2f s\n" % (end_time - start_time)

    # python UserCF_Spark.py --input ./data/spark_ratings_100k.csv
