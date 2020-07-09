import time, sys
from operator import itemgetter
import numpy as np
from ..utils.similarities import *
from ..utils.baseline_estimates import baseline_als, baseline_sgd
from .base import BasePure
try:
    from ..utils.similarities_cy import cosine_cy, pearson_cy
except ImportError:
    pass
import logging
LOG_FORMAT = "%(asctime)s - %(levelname)s - %(message)s"
logging.basicConfig(format=LOG_FORMAT)
logging.warning("Item KNN method requires huge memory for constructing similarity matrix. \n"
                "\tFor large num of users or items, consider using sklearn sim_option, "
                "which provides sparse similarity matrix. \n")


class itemCF(BasePure):
    def __init__(self, sim_option="pearson", k=50, min_support=1, baseline=True, task="rating", neg_sampling=False):
        self.k = k
        self.min_support = min_support
        self.baseline = baseline
        self.task = task
        self.neg_sampling = neg_sampling
        if sim_option == "cosine":
            self.sim_option = cosine_cy  # cosine_sim
        elif sim_option == "pearson":
            self.sim_option = pearson_sim  # pearson_cy
        elif sim_option == "sklearn":
          self.sim_option = sk_sim
        else:
            raise ValueError("sim_option %s not allowed" % sim_option)
        super(itemCF, self).__init__()

    def fit(self, dataset, verbose=1, **kwargs):
        self.dataset = dataset
        self.global_mean = dataset.global_mean
        self.train_user = dataset.train_user
        self.train_item = dataset.train_item
        if dataset.lower_upper_bound is not None:
            self.lower_bound = dataset.lower_upper_bound[0]
            self.upper_bound = dataset.lower_upper_bound[1]
        else:
            self.lower_bound = None
            self.upper_bound = None

        t0 = time.time()
        if self.sim_option == sk_sim:
            self.sim = self.sim_option(self.train_user, dataset.n_users, dataset.n_items,
                                       min_support=self.min_support, sparse=True)
        else:
            user_item_list = {k: list(v.items()) for k, v in dataset.train_user.items()}
            self.sim = self.sim_option(dataset.n_items, user_item_list, min_support=self.min_support)
            self.sim = np.array(self.sim)

        #    n = len(self.train_item)
        #    ids = list(self.train_item.keys())
        #    self.sim = get_sim(self.train_item, self.sim_option, n, ids, min_support=self.min_support)

        print("sim time: {:.4f}, sim shape: {}".format(time.time() - t0, self.sim.shape))
        if issparse(self.sim):
            print("sim num_elements: {}".format(self.sim.getnnz()))
        if self.baseline and self.task == "rating":
            self.bu, self.bi = baseline_als(dataset)

        if verbose > 0:
            print("training_time: {:.2f}".format(time.time() - t0))
            metrics = kwargs.get("metrics", self.metrics)
            if hasattr(self, "sess"):
                self.print_metrics_tf(dataset, 1, **metrics)
            else:
                self.print_metrics(dataset, 1, **metrics)
            print()

        return self

    def predict(self, u, i):
        if self.sim_option == sk_sim:
            i_nonzero_neighbors = set(self.sim.rows[i])
        else:
            i_nonzero_neighbors = set(np.where(self.sim[i] != 0.0)[0])

        try:
            neighbors = [(j, self.sim[i, j], r) for (j, r) in self.train_user[u].items()
                         if j in i_nonzero_neighbors and i != j]
            k_neighbors = sorted(neighbors, key=lambda x: x[1], reverse=True)[:self.k]
            if self.baseline and self.task == "rating":
                bui = self.global_mean + self.bu[u] + self.bi[i]
        except IndexError:
            return self.global_mean if self.task == "rating" else 0.0
        if len(neighbors) == 0:
            return self.global_mean if self.task == "rating" else 0.0

        if self.task == "rating":
            sim_ratings = 0
            sim_sums = 0
            for (j, sim, r) in k_neighbors:
                if sim > 0 and self.baseline:
                    buj = self.global_mean + self.bu[u] + self.bi[j]
                    sim_ratings += sim * (r - buj)
                    sim_sums += sim
                elif self.sim > 0:
                    sim_ratings += sim * r
                    sim_sums += sim

            try:
                if self.baseline:
                    pred = bui + sim_ratings / sim_sums
                else:
                    pred = sim_ratings / sim_sums

                if self.lower_bound is not None and self.upper_bound is not None:
                    pred = np.clip(pred, self.lower_bound, self.upper_bound)
                return pred
            except ZeroDivisionError:
                print("item %d sim item is zero" % u)
                return self.global_mean

        elif self.task == "ranking":
            sim_sums = 0
            for (_, sim, r) in k_neighbors:
                if sim > 0:
                    sim_sums += sim
            return sim_sums

    def recommend_user(self, u, n_rec, like_score=4.0, random_rec=False):
        rank = set()
        u_items = np.array(list(self.train_user[u].items()))
        if self.task == "rating":
            u_items = [(i, r) for i, r in u_items if r >= like_score]

        for i, _ in u_items:
            i = int(i)
            if self.sim_option == sk_sim and len(self.sim.rows[i]) <= 1:  # no neighbors, just herself
                continue
            elif self.sim_option != sk_sim and len(np.where(self.sim[i] != 0.0)[0]) <= 1:
                continue

            if self.sim_option == sk_sim:
                indices = np.argsort(self.sim[i].data[0])[::-1][1: self.k + 1]
                k_neighbors = np.array(self.sim.rows[i])[indices]
            else:
                k_neighbors = np.argsort(self.sim[i])[::-1][1: self.k + 1]

            for j in k_neighbors:
                if j in self.train_user[u]:
                    continue
                pred = self.predict(u, j)
                rank.add((int(j), pred))
        rank = list(rank)

        if random_rec:
            item_pred_dict = {j: pred for j, pred in rank if pred >= like_score}
            if len(item_pred_dict) == 0:
                print("not enough candidates, try raising the like_score")
                sys.exit(1)

            item_list = list(item_pred_dict.keys())
            pred_list = list(item_pred_dict.values())
            p = [p / np.sum(pred_list) for p in pred_list]

            if len(item_list) < n_rec:
                item_candidates = item_list
            else:
                item_candidates = np.random.choice(item_list, n_rec, replace=False, p=p)
            reco = [(item, item_pred_dict[item]) for item in item_candidates]
            return reco
        else:
            rank.sort(key=itemgetter(1), reverse=True)
            return rank[:n_rec]



















