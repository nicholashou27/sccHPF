import numpy as np
import pandas as pd
import os, errno
import datetime
import uuid
import itertools
import sklearn
import schpf
import scipy.sparse as sp
import scanpy as sc

from scipy.spatial.distance import squareform
from sklearn.decomposition import non_negative_factorization
from sklearn.cluster import KMeans
from sklearn.metrics import silhouette_score, silhouette_samples

from fastcluster import linkage
from scipy.cluster.hierarchy import leaves_list

import matplotlib.pyplot as plt
import matplotlib.cm as cm

def save_df_to_npz(obj, filename):
    np.savez_compressed(filename, data=obj.values, index=obj.index.values, columns=obj.columns.values)

def save_df_to_text(obj, filename):
    obj.to_csv(filename, sep='\t')

def load_df_from_npz(filename):
    with np.load(filename, allow_pickle=True) as f:
        obj = pd.DataFrame(**f)
    return obj

def check_dir_exists(path):
    """
    Checks if directory already exists or not and creates it if it doesn't
    """
    try:
        os.makedirs(path)
    except OSError as exception:
        if exception.errno != errno.EEXIST:
            raise

def worker_filter(iterable, worker_index, total_workers):
    return (p for i,p in enumerate(iterable) if (i-worker_index)%total_workers==0)

def fast_euclidean(mat):
    D = mat.dot(mat.T)
    squared_norms = np.diag(D).copy()
    D *= -2.0
    D += squared_norms.reshape((-1,1))
    D += squared_norms.reshape((1,-1))
    D = np.sqrt(D)
    D[D < 0] = 0
    return squareform(D, checks=False)

def fast_ols_all_cols(X, Y):
    pinv = np.linalg.pinv(X)
    beta = np.dot(pinv, Y)
    return(beta)

def fast_ols_all_cols_df(X,Y):
    beta = fast_ols_all_cols(X, Y)
    beta = pd.DataFrame(beta, index=X.columns, columns=Y.columns)
    return(beta)

def get_high_var_genes(input_counts, expected_fano_threshold=None,
                       minimal_mean=0.01, numgenes=None):
    '''
    Calculate the expected Fano Factor of a gene based on its Mean. Calculate
    the ratio of the Observed Fano Factor over the expected. Either select all
    genes with this ratio greather than a threshold, or take the top K genes
    with the highest value of this ratio.

    The expected relationship between mean and fano is fit as a linear
    relationship on a bin of the data between the 10th and 90th quantiles
    for both values.
    '''

    # Find high variance genes within cells
    gene_counts_mean = input_counts.mean().astype(float)
    gene_counts_var = input_counts.var(ddof=0).astype(float)
    gene_counts_fano = gene_counts_var/gene_counts_mean

    # Find parameters for expected fano line
    top_genes = gene_counts_mean.sort_values(ascending=False)[:20].index
    A = (np.sqrt(gene_counts_var)/gene_counts_mean)[top_genes].min()

    w_mean_low, w_mean_high = gene_counts_mean.quantile([0.10, 0.90])
    w_fano_low, w_fano_high = gene_counts_fano.quantile([0.10, 0.90])
    winsor_box = ((gene_counts_fano > w_fano_low) &
                    (gene_counts_fano < w_fano_high) &
                    (gene_counts_mean > w_mean_low) &
                    (gene_counts_mean < w_mean_high))
    fano_median = gene_counts_fano[winsor_box].median()
    B = np.sqrt(fano_median)

    gene_expected_fano = (A**2)*gene_counts_mean + (B**2)

    fano_ratio = (gene_counts_fano/gene_expected_fano)

    # Identify high var genes
    if numgenes is not None:
        highvargenes = fano_ratio.sort_values(ascending=False).index[:numgenes]
        high_var_genes_ind = fano_ratio.index.isin(highvargenes)
        T=None

    else:
        if not expected_fano_threshold:
            T = (1. + gene_counts_fano[winsor_box].std())
        else:
            T = expected_fano_threshold

        high_var_genes_ind = (fano_ratio > T) & (gene_counts_mean > minimal_mean)

    gene_counts_stats = pd.DataFrame({
        'mean': gene_counts_mean,
        'var': gene_counts_var,
        'fano': gene_counts_fano,
        'expected_fano': gene_expected_fano,
        'high_var': high_var_genes_ind,
        'fano_ratio': fano_ratio
        })
    gene_fano_parameters = {
            'A': A, 'B': B, 'T':T, 'minimal_mean': minimal_mean,
        }
    return(gene_counts_stats, gene_fano_parameters)


def compute_tpm(input_counts):
    """
    Default TPM normalization
    """
    return(input_counts.div(input_counts.sum(axis=1), axis=0) * (10**6))


class cNMF():


    def __init__(self, output_dir=".", name=None):
        """
        Parameters
        ----------

        output_dir : path, optional (default=".")
            Output directory for analysis files.

        name : string, optional (default=None)
            A name for this analysis. Will be prefixed to all output files.
            If set to None, will be automatically generated from date (and random string).
        """

        self.output_dir = output_dir
        if name is None:
            now = datetime.datetime.now()
            rand_hash =  uuid.uuid4().hex[:6]
            name = '%s_%s' % (now.strftime("%Y_%m_%d"), rand_hash)
        self.name = name
        self.paths = None


    def _initialize_dirs(self):
        if self.paths is None:
            # Check that output directory exists, create it if needed.
            check_dir_exists(self.output_dir)
            check_dir_exists(os.path.join(self.output_dir, self.name))
            check_dir_exists(os.path.join(self.output_dir, self.name, 'cnmf_tmp'))

            self.paths = {
                'normalized_counts' : os.path.join(self.output_dir, self.name, 'cnmf_tmp', self.name+'.norm_counts.df.npz'),
                'nmf_parameters' :  os.path.join(self.output_dir, self.name, 'cnmf_tmp', self.name+'.nmf_params.df.npz'),
                'tpm' :  os.path.join(self.output_dir, self.name, 'cnmf_tmp', self.name+'.tpm.df.npz'),
                'tpm_stats' :  os.path.join(self.output_dir, self.name, 'cnmf_tmp', self.name+'.tpm_stats.df.npz'),

                'train_normalized_counts' : os.path.join(self.output_dir, self.name, 'cnmf_tmp', self.name+'.train_norm_counts.df.npz'),
                'train_nmf_parameters' :  os.path.join(self.output_dir, self.name, 'cnmf_tmp', self.name+'.train_nmf_params.df.npz'),
                'train_tpm' :  os.path.join(self.output_dir, self.name, 'cnmf_tmp', self.name+'.train_tpm.df.npz'),
                'train_tpm_stats' :  os.path.join(self.output_dir, self.name, 'cnmf_tmp', self.name+'.train_tpm_stats.df.npz'),

                'iter_spectra' :  os.path.join(self.output_dir, self.name, 'cnmf_tmp', self.name+'.spectra.k_%d.iter_%d.df.npz'),
                'iter_usages' :  os.path.join(self.output_dir, self.name, 'cnmf_tmp', self.name+'.usages.k_%d.iter_%d.df.npz'),
                'iter_beta_shape' :  os.path.join(self.output_dir, self.name, 'cnmf_tmp', self.name+'.beta_shape.k_%d.iter_%d.df.npz'),
                'iter_beta_rate' :  os.path.join(self.output_dir, self.name, 'cnmf_tmp', self.name+'.beta_rate.k_%d.iter_%d.df.npz'),
                'iter_eta_shape' :  os.path.join(self.output_dir, self.name, 'cnmf_tmp', self.name+'.eta_shape.k_%d.iter_%d.df.npz'),
                'iter_eta_rate' :  os.path.join(self.output_dir, self.name, 'cnmf_tmp', self.name+'.eta_rate.k_%d.iter_%d.df.npz'),
                
                'merged_spectra': os.path.join(self.output_dir, self.name, 'cnmf_tmp', self.name+'.spectra.k_%d.merged.df.npz'),
                'merged_usages': os.path.join(self.output_dir, self.name, 'cnmf_tmp', self.name+'.usages.k_%d.merged.df.npz'),
                'merged_beta_shape': os.path.join(self.output_dir, self.name, 'cnmf_tmp', self.name+'.beta_shape.k_%d.merged.df.npz'),
                'merged_beta_rate': os.path.join(self.output_dir, self.name, 'cnmf_tmp', self.name+'.beta_rate.k_%d.merged.df.npz'),
                'merged_eta_shape': os.path.join(self.output_dir, self.name, 'cnmf_tmp', self.name+'.eta_shape.k_%d.merged.df.npz'),
                'merged_eta_rate': os.path.join(self.output_dir, self.name, 'cnmf_tmp', self.name+'.eta_rate.k_%d.merged.df.npz'),

                'local_density_cache': os.path.join(self.output_dir, self.name, 'cnmf_tmp', self.name+'.local_density_cache.k_%d.merged.df.npz'),
                'consensus_spectra': os.path.join(self.output_dir, self.name, 'cnmf_tmp', self.name+'.spectra.k_%d.dt_%s.consensus.df.npz'),
                'consensus_spectra__txt': os.path.join(self.output_dir, self.name, self.name+'.spectra.k_%d.dt_%s.consensus.txt'),
                'consensus_usages': os.path.join(self.output_dir, self.name, 'cnmf_tmp',self.name+'.usages.k_%d.dt_%s.consensus.df.npz'),
                'consensus_usages__txt': os.path.join(self.output_dir, self.name, self.name+'.usages.k_%d.dt_%s.consensus.txt'),

                'consensus_stats': os.path.join(self.output_dir, self.name, 'cnmf_tmp', self.name+'.stats.k_%d.dt_%s.df.npz'),

                'clustering_plot': os.path.join(self.output_dir, self.name, self.name+'.clustering.k_%d.dt_%s.pdf'),
                'silhouette_plot': os.path.join(self.output_dir, self.name, self.name+'.clustering.k_%d.pdf'),
                'gene_spectra_score': os.path.join(self.output_dir, self.name, 'cnmf_tmp', self.name+'.gene_spectra_score.k_%d.dt_%s.df.npz'),
                'gene_spectra_score__txt': os.path.join(self.output_dir, self.name, self.name+'.gene_spectra_score.k_%d.dt_%s.txt'),
                'gene_spectra_tpm': os.path.join(self.output_dir, self.name, 'cnmf_tmp', self.name+'.gene_spectra_tpm.k_%d.dt_%s.df.npz'),
                'gene_spectra_tpm__txt': os.path.join(self.output_dir, self.name, self.name+'.gene_spectra_tpm.k_%d.dt_%s.txt'),

                'k_selection_plot' :  os.path.join(self.output_dir, self.name, self.name+'.k_selection.pdf'),
                'k_selection_stats' :  os.path.join(self.output_dir, self.name, self.name+'.k_selection_stats.df.npz'),
            }


    def get_norm_counts(self, counts_df,
                        high_variance_genes_filter = None,
                        num_highvar_genes = None,
                        train_set=False,
                        train_counts_df=None
                         ):
        """
        Parameters
        ----------

        counts_df : pandas.DataFrame,
            Single-cell sequencing counts (cells x genes), after preprocessing to
            remove cells with low quality or too few counts.

        high_variance_genes_filter : np.array, optional (default=None)
            A pre-specified list of genes considered to be high-variance.
            Only these genes will be used during factorization of the counts matrix.
            If set to None, high-variance genes will be automatically computed, using the parameters below.

        """
        if high_variance_genes_filter is None:
            tpm_df = counts_df.div(counts_df.sum(axis=1), axis=0)*(10**6)
            gene_counts_stats, gene_fano_params = get_high_var_genes(tpm_df, numgenes=num_highvar_genes)
            high_variance_genes_filter = gene_counts_stats.high_var
        high_var_counts = counts_df.loc[:, high_variance_genes_filter]
        # norm_counts = high_var_counts/high_var_counts.std()
        norm_counts = high_var_counts
        norm_counts = norm_counts.fillna(0.0)

        if train_set: 
            train_high_var_counts = train_counts_df.loc[:, high_variance_genes_filter]
            # train_norm_counts = train_high_var_counts/train_high_var_counts.std()
            train_norm_counts = train_high_var_counts
            train_norm_counts = train_norm_counts.fillna(0.0)
            
            return norm_counts, train_norm_counts 
        else:                   
            return norm_counts

    def save_norm_counts(self, norm_counts_df):
        self._initialize_dirs()
        save_df_to_npz(norm_counts_df, self.paths['normalized_counts'])

    def save_train_norm_counts(self, train_norm_counts_df):
        self._initialize_dirs()
        save_df_to_npz(train_norm_counts_df, self.paths['train_normalized_counts'])

    def get_nmf_iter_params(self, ks, n_iter = 100,
                               random_state_seed = None):
        """
        Create a DataFrame with parameters for NMF iterations.


        Parameters
        ----------
        ks : integer, or list-like.
            Number of topics (components) for factorization.
            Several values can be specified at the same time, which will be run independently.

        n_iter : integer, optional (defailt=100)
            Number of iterations for factorization. If several ``k`` are specified, this many
            iterations will be run for each value of ``k``.

        random_state_seed : int or None, optional (default=None)
            Seed for sklearn random state.

        """

        if type(ks) is int:
            ks = [ks]

        # Remove any repeated k values, and order.
        k_list = sorted(set(list(ks)))


        #random_state = sklearn.utils.check_random_state(random_state_seed)

        n_runs = len(ks)* n_iter
        #nmf_seeds = random_state.randint(0, np.iinfo(np.int32).max+1, size=n_runs)

        np.random.seed(seed=random_state_seed)
        nmf_seeds = np.random.randint(low=1, high=(2**32)-1, size=n_runs)


        run_params = []
        for i, (k, r) in enumerate(itertools.product(k_list, range(n_iter))):
            run_params.append([k, r, nmf_seeds[i]])
        run_params = pd.DataFrame(run_params, columns = ['n_components', 'iter', 'nmf_seed'])

        return run_params

    def save_nmf_iter_params(self, run_params):
        self._initialize_dirs()
        save_df_to_npz(run_params, self.paths['nmf_parameters'])


    def _nmf(self, X, nmf_kwargs, topic_labels=None, train_set=False, train_X=None): # EDIT 10/12/23
        """
        Parameters
        ----------
        X : pandas.DataFrame,
            Normalized counts dataFrame to be factorized.

        nmf_kwargs : dict,
            Arguments to be passed to ``non_negative_factorization``

        """
        # (W, H, niter) = non_negative_factorization(X.values, **nmf_kwargs)

        adata = sc.AnnData(X)

        # EDIT 10/12/23
        if train_set:
            train_adata = sc.AnnData(train_X)
            hpf_model = schpf.run_trials(sp.coo_matrix(train_adata.X),nmf_kwargs['n_components'],ntrials=1,epsilon=0.001)
            hpf_model.project(sp.coo_matrix(adata.X), replace=True)
        else:
            hpf_model = schpf.run_trials(sp.coo_matrix(adata.X),nmf_kwargs['n_components'],ntrials=1,epsilon=0.001)
        
        # (W, H) = hpf_model.cell_score(), hpf_model.gene_score()
        H = hpf_model.gene_score()

        # usages = pd.DataFrame(W, index=X.index, columns=topic_labels)
        spectra = pd.DataFrame(np.transpose(H), columns=X.columns, index=topic_labels)
        
        beta_shape = pd.DataFrame(np.transpose(hpf_model.beta.vi_shape), columns = X.columns, index=topic_labels)
        beta_rate = pd.DataFrame(np.transpose(hpf_model.beta.vi_rate), columns = X.columns, index=topic_labels)
        
        eta_shape = pd.DataFrame(hpf_model.eta.vi_shape)
        eta_shape = pd.concat([eta_shape] * spectra.shape[0], axis=1, ignore_index=True)
        eta_shape = eta_shape.T
        eta_shape.columns = X.columns 
        
        eta_rate = pd.DataFrame(hpf_model.eta.vi_rate) 
        eta_rate = pd.concat([eta_rate] * spectra.shape[0], axis=1, ignore_index=True)
        eta_rate = eta_rate.T
        eta_rate.columns = X.columns 

        #Sort by overall usage, and rename topics with 1-indexing.
        topic_order = spectra.sum(axis=1).sort_values(ascending=False).index

        spectra = spectra.loc[topic_order, :]
        # usages = usages.loc[:, topic_order]

        beta_shape = beta_shape.loc[topic_order, :]
        beta_rate = beta_rate.loc[topic_order, :]
        
        eta_shape = eta_shape.loc[topic_order, :]
        eta_rate = eta_rate.loc[topic_order, :]

        if topic_labels is None:
            spectra.index = np.arange(1, nmf_kwargs['n_components']+1)
            # usages.columns = np.arange(1, nmf_kwargs['n_components']+1)
            beta_shape.index = np.arange(1, nmf_kwargs['n_components']+1)
            beta_rate.index = np.arange(1, nmf_kwargs['n_components']+1)
            eta_shape.index = np.arange(1, nmf_kwargs['n_components']+1)
            eta_rate.index = np.arange(1, nmf_kwargs['n_components']+1)

        return spectra, beta_shape, beta_rate, eta_shape, eta_rate


    def run_nmf(self,
                nmf_kwargs = dict(),
                worker_i=1, total_workers=1,
                train_set=False, # EDIT 10/12/23
                ):
        """
        Iteratively run NMF with prespecified parameters.

        Use the `worker_i` and `total_workers` parameters for parallelization.

        Parameters
        ----------
        norm_counts : pandas.DataFrame,
            Normalized counts dataFrame to be factorized.
            (Output of ``normalize_counts``)

        run_params : pandas.DataFrame,
            Parameters for NMF iterations.
            (Output of ``prepare_nmf_iter_params``)

        nmf_kwargs = dict, optional (default: {})
            kwargs to be passed to ``non_negative_factorization``, updating the defaults below.

            ``non_negative_factorization`` default arguments:
                alpha=0.0
                l1_ratio=0.0
                beta_loss='frobenius'
                solver='cd'
                tol=1e-4,
                max_iter=200
                regularization=None

                random_state, n_components are both set by the prespecified run_params.

        """
        self._initialize_dirs()
        run_params = load_df_from_npz(self.paths['nmf_parameters'])
        norm_counts = load_df_from_npz(self.paths['normalized_counts'])
        if train_set:
            print('Training with projection...')
            train_norm_counts = load_df_from_npz(self.paths['train_normalized_counts'])
        else:
            print('Training...')
            train_norm_counts = None 

        _nmf_kwargs = dict(
            alpha=0.0,
            l1_ratio=0.0,
            beta_loss='frobenius',
            solver='cd',
            tol=1e-4,
            max_iter=400,
            regularization=None,
        )
        _nmf_kwargs.update(nmf_kwargs)

        jobs_for_this_worker = worker_filter(range(len(run_params)), worker_i, total_workers)
        for idx in jobs_for_this_worker:

            p = run_params.iloc[idx, :]
            print('[Worker %d]. Starting task %d.' % (worker_i, idx))
            _nmf_kwargs['random_state'] = p['nmf_seed']
            _nmf_kwargs['n_components'] = p['n_components']

            # 10/12/23 EDIT 
            spectra, beta_shape, beta_rate, eta_shape, eta_rate = self._nmf(norm_counts, _nmf_kwargs, 
                                                                            train_set=train_set, 
                                                                            train_X=train_norm_counts) 

            save_df_to_npz(spectra, self.paths['iter_spectra'] % (p['n_components'], p['iter']))
            # save_df_to_npz(usages, self.paths['iter_usages'] % (p['n_components'],p['iter']))

            save_df_to_npz(beta_shape, self.paths['iter_beta_shape'] % (p['n_components'], p['iter']))
            save_df_to_npz(beta_rate, self.paths['iter_beta_rate'] % (p['n_components'],p['iter']))

            save_df_to_npz(eta_shape, self.paths['iter_eta_shape'] % (p['n_components'], p['iter']))
            save_df_to_npz(eta_rate, self.paths['iter_eta_rate'] % (p['n_components'],p['iter']))


    def combine_nmf(self, k, remove_individual_iterations=False):
        run_params = load_df_from_npz(self.paths['nmf_parameters'])
        print('Combining factorizations for k=%d.'%k)

        self._initialize_dirs()

        combined_spectra = None
        # combined_usages = None
        combined_beta_shape = None
        combined_beta_rate = None
        combined_eta_shape = None
        combined_eta_rate = None 
        n_iter = sum(run_params.n_components==k)

        run_params_subset = run_params[run_params.n_components==k].sort_values('iter')
        spectra_labels = []
        # usages_labels = []
        beta_shape_labels = []
        beta_rate_labels = []
        eta_shape_labels = []
        eta_rate_labels = []

        for i,p in run_params_subset.iterrows():

            spectra = load_df_from_npz(self.paths['iter_spectra'] % (p['n_components'], p['iter']))
            if combined_spectra is None:
                combined_spectra = np.zeros((n_iter, k, spectra.shape[1]))
            combined_spectra[p['iter'], :, :] = spectra.values

            beta_shape = load_df_from_npz(self.paths['iter_beta_shape'] % (p['n_components'], p['iter']))
            if combined_beta_shape is None:
                combined_beta_shape = np.zeros((n_iter, k, beta_shape.shape[1]))
            combined_beta_shape[p['iter'], :, :] = beta_shape.values

            beta_rate = load_df_from_npz(self.paths['iter_beta_rate'] % (p['n_components'], p['iter']))
            if combined_beta_rate is None:
                combined_beta_rate = np.zeros((n_iter, k, beta_rate.shape[1]))
            combined_beta_rate[p['iter'], :, :] = beta_rate.values

            eta_shape = load_df_from_npz(self.paths['iter_eta_shape'] % (p['n_components'], p['iter']))
            if combined_eta_shape is None:
                combined_eta_shape = np.zeros((n_iter, k, eta_shape.shape[1]))
            combined_eta_shape[p['iter'], :, :] = eta_shape.values

            eta_rate = load_df_from_npz(self.paths['iter_eta_rate'] % (p['n_components'], p['iter']))
            if combined_eta_rate is None:
                combined_eta_rate = np.zeros((n_iter, k, eta_rate.shape[1]))
            combined_eta_rate[p['iter'], :, :] = eta_rate.values

            """
            usages = load_df_from_npz(self.paths['iter_usages'] % (p['n_components'], p['iter']))
            if combined_usages is None:
                combined_usages = np.zeros((n_iter, usages.shape[0], k))
            combined_usages[p['iter'], :, :] = usages.values
            """

            for t in range(k):
                spectra_labels.append('iter%d_topic%d'%(p['iter'], t+1))
                # usages_labels.append('iter%d_topic%d'%(p['iter'], t+1))
                beta_shape_labels.append('iter%d_topic%d'%(p['iter'], t+1))
                beta_rate_labels.append('iter%d_topic%d'%(p['iter'], t+1))
                eta_shape_labels.append('iter%d_topic%d'%(p['iter'], t+1))
                eta_rate_labels.append('iter%d_topic%d'%(p['iter'], t+1))

        combined_spectra = combined_spectra.reshape(-1, combined_spectra.shape[-1])
        combined_spectra = pd.DataFrame(combined_spectra, columns=spectra.columns, index=spectra_labels)
        save_df_to_npz(combined_spectra, self.paths['merged_spectra']%k)

        combined_beta_shape = combined_beta_shape.reshape(-1, combined_beta_shape.shape[-1])
        combined_beta_shape = pd.DataFrame(combined_beta_shape, columns=beta_shape.columns, index=beta_shape_labels)
        save_df_to_npz(combined_beta_shape, self.paths['merged_beta_shape']%k)

        combined_beta_rate = combined_beta_rate.reshape(-1, combined_beta_rate.shape[-1])
        combined_beta_rate = pd.DataFrame(combined_beta_rate, columns=beta_rate.columns, index=beta_rate_labels)
        save_df_to_npz(combined_beta_rate, self.paths['merged_beta_rate']%k)

        combined_eta_shape = combined_eta_shape.reshape(-1, combined_eta_shape.shape[-1])
        combined_eta_shape = pd.DataFrame(combined_eta_shape, columns=eta_shape.columns, index=eta_shape_labels)
        save_df_to_npz(combined_eta_shape, self.paths['merged_eta_shape']%k)

        combined_eta_rate = combined_eta_rate.reshape(-1, combined_eta_rate.shape[-1])
        combined_eta_rate = pd.DataFrame(combined_eta_rate, columns=eta_rate.columns, index=eta_rate_labels)
        save_df_to_npz(combined_eta_rate, self.paths['merged_eta_rate']%k)

        """
        combined_usages = combined_usages.reshape(-1, combined_usages.shape[-2]).T
        combined_usages = pd.DataFrame(combined_usages, columns=usages_labels, index=usages.index)
        save_df_to_npz(combined_usages, self.paths['merged_usages']%k)
        """
        
        return combined_spectra, combined_beta_shape, combined_beta_rate, combined_eta_shape, combined_eta_rate


    def consensus(self, k, density_threshold_str='0.5', local_neighborhood_size = 0.30,show_clustering = False, skip_density_and_return_after_stats = False, close_clustergram_fig=True,
                train_set=False): # EDIT 10/12/23
        merged_spectra = load_df_from_npz(self.paths['merged_spectra']%k)
        merged_beta_shape = load_df_from_npz(self.paths['merged_beta_shape']%k)
        merged_beta_rate = load_df_from_npz(self.paths['merged_beta_rate']%k)
        merged_eta_shape = load_df_from_npz(self.paths['merged_eta_shape']%k)
        merged_eta_rate = load_df_from_npz(self.paths['merged_eta_rate']%k)
        norm_counts = load_df_from_npz(self.paths['normalized_counts'])

        def median_index(lst):
            sorted_lst = sorted(lst)
            mid = len(lst) // 2
            median = sorted_lst[mid]
            return lst.index(median)

        if skip_density_and_return_after_stats:
            density_threshold_str = '2'
        density_threshold_repl = density_threshold_str.replace('.', '_')
        density_threshold = float(density_threshold_str)
        n_neighbors = int(local_neighborhood_size * merged_spectra.shape[0]/k)

        # Rescale topics such to length of 1.
        l2_spectra = (merged_spectra.T/np.sqrt((merged_spectra**2).sum(axis=1))).T

        if not skip_density_and_return_after_stats:
            # Compute the local density matrix (if not previously cached)
            topics_dist = None
            if os.path.isfile(self.paths['local_density_cache'] % k):
                local_density = load_df_from_npz(self.paths['local_density_cache'] % k)
            else:
                #   first find the full distance matrix
                topics_dist = squareform(fast_euclidean(l2_spectra.values))
                #   partition based on the first n neighbors
                partitioning_order  = np.argpartition(topics_dist, n_neighbors+1)[:, :n_neighbors+1]
                #   find the mean over those n_neighbors (excluding self, which has a distance of 0)
                distance_to_nearest_neighbors = topics_dist[np.arange(topics_dist.shape[0])[:, None], partitioning_order]
                if n_neighbors == 0:
                    local_density = pd.DataFrame(distance_to_nearest_neighbors.sum(1)/1,
                                                columns=['local_density'],
                                                index=l2_spectra.index)
                else:
                    local_density = pd.DataFrame(distance_to_nearest_neighbors.sum(1)/(n_neighbors),
                                                columns=['local_density'],
                                                index=l2_spectra.index)
                save_df_to_npz(local_density, self.paths['local_density_cache'] % k)
                del(partitioning_order)
                del(distance_to_nearest_neighbors)

            density_filter = local_density.iloc[:, 0] < density_threshold
            # l2_spectra = l2_spectra.loc[density_filter, :]

        kmeans_model = KMeans(n_clusters=k, n_init=10, random_state=1)
        kmeans_model.fit(l2_spectra)
        kmeans_cluster_labels = pd.Series(kmeans_model.labels_+1, index=l2_spectra.index)

        # Compute the silhouette score
        stability = silhouette_score(l2_spectra.values, kmeans_cluster_labels, metric='euclidean')

        # Determine the factor replicates that produce the median L2_spectra scores in each gene across KMeans clusters 
        median_replicates_df = pd.DataFrame(0,columns=l2_spectra.columns,index=[i for i in range(1,k+1)])
        median_spectra = pd.DataFrame(0,columns=l2_spectra.columns,index=[i for i in range(1,k+1)])
        median_beta_shape = pd.DataFrame(0,columns=l2_spectra.columns,index=[i for i in range(1,k+1)])
        median_beta_rate = pd.DataFrame(0,columns=l2_spectra.columns,index=[i for i in range(1,k+1)])
        median_eta_shape = pd.DataFrame(0,columns=l2_spectra.columns,index=[i for i in range(1,k+1)])
        median_eta_rate = pd.DataFrame(0,columns=l2_spectra.columns,index=[i for i in range(1,k+1)])
        
        for i in range(l2_spectra.shape[1]): # for every gene 
            df = pd.DataFrame()
            df['replicate'] = l2_spectra.index.tolist()
            df['kmeans_cluster_labels'] = kmeans_cluster_labels.tolist()
            df['spectra_score'] = l2_spectra.iloc[:,i].tolist()
            df['beta_shape_score'] = merged_beta_shape.iloc[:,i].tolist()
            df['beta_rate_score'] = merged_beta_rate.iloc[:,i].tolist()
            df['eta_shape_score'] = merged_eta_shape.iloc[:,i].tolist()
            df['eta_rate_score'] = merged_eta_rate.iloc[:,i].tolist()
        
            median_replicates = []
            median_spectra_col = []
            median_beta_shape_col = []
            median_beta_rate_col = []
            median_eta_shape_col = []
            median_eta_rate_col = []
        
            for c in range(1,k+1):
                idx = median_index(df[df['kmeans_cluster_labels']==c]['spectra_score'].tolist())
                median_replicates.append(df[df['kmeans_cluster_labels']==c]['replicate'].tolist()[idx])
                median_spectra_col.append(df[df['kmeans_cluster_labels']==c]['spectra_score'].tolist()[idx])
                median_beta_shape_col.append(df[df['kmeans_cluster_labels']==c]['beta_shape_score'].tolist()[idx])
                median_beta_rate_col.append(df[df['kmeans_cluster_labels']==c]['beta_rate_score'].tolist()[idx])
                median_eta_shape_col.append(df[df['kmeans_cluster_labels']==c]['eta_shape_score'].tolist()[idx])
                median_eta_rate_col.append(df[df['kmeans_cluster_labels']==c]['eta_rate_score'].tolist()[idx])
            
            median_replicates_df.iloc[:,i] = median_replicates
            median_spectra.iloc[:,i] = median_spectra_col
            median_beta_shape.iloc[:,i] = median_beta_shape_col
            median_beta_rate.iloc[:,i] = median_beta_rate_col
            median_eta_shape.iloc[:,i] = median_eta_shape_col
            median_eta_rate.iloc[:,i] = median_eta_rate_col
        
        median_eta_shape = median_eta_shape.mean(axis=0)
        median_eta_rate = median_eta_rate.mean(axis=0)

        # Create scHPF model with the median beta and eta 
        consensus_beta = schpf.HPF_Gamma(np.transpose(median_beta_shape.to_numpy()),
                                         np.transpose(median_beta_rate.to_numpy()))
        consensus_eta = schpf.HPF_Gamma(np.transpose(median_eta_shape.to_numpy()),
                                        np.transpose(median_eta_rate.to_numpy()))
        
        consensus_HPF = schpf.scHPF(nfactors=k,
                                    beta=consensus_beta,
                                    eta=consensus_eta)

        # Fit theta and xi to the consensus beta and eta
        X = sp.coo_matrix(sc.AnnData(norm_counts).X)
        consensus_HPF.bp, consensus_HPF.dp = consensus_HPF._get_empirical_hypers(X) # assign empirical hyperparameters for the projected dataset
        
        (bp, _, xi, _, theta, _, loss) = consensus_HPF._fit(X, freeze_genes=True)
        consensus_HPF.xi = xi
        consensus_HPF.theta = theta

        topic_labels=np.arange(1,k+1)

        # Obtain the consensus GEP and cell usage matrices from the model 
        (W, H) = consensus_HPF.cell_score(), consensus_HPF.gene_score()
        consensus_usages = pd.DataFrame(W, index=norm_counts.index, columns=topic_labels)
        consensus_spectra = pd.DataFrame(np.transpose(H), columns=norm_counts.columns, index=topic_labels)

        if topic_labels is None:
            consensus_spectra.index = np.arange(1, nmf_kwargs['n_components']+1)
            consensus_usages.columns = np.arange(1, nmf_kwargs['n_components']+1)

        # Normalize consensus spectra to probability distributions.
        # consensus_spectra = (consensus_spectra.T/consensus_spectra.sum(1)).T

        # Obtain the reconstructed count matrix by re-fitting the usage matrix and computing the dot product: usage.dot(spectra)
        refit_nmf_kwargs = dict(
            n_components = k,
            H = median_spectra.values,
            update_H = False,
            shuffle = True,

            alpha=0.0,
            l1_ratio=0.0,
            beta_loss='frobenius',
            solver='cd',
            tol=1e-4,
            max_iter=1000,
            regularization=None,
        )
            
        nmf_kwargs=refit_nmf_kwargs

        # Posterior predictive check should be ran according to the appropriate training data
        if not train_set: 
            rf_pred_norm_counts = consensus_usages.dot(consensus_spectra)
            # Compute prediction error as a frobenius norm
            frobenius_error = ((norm_counts - rf_pred_norm_counts)**2).sum().sum()
        else: 
            train_consensus_HPF = schpf.scHPF(nfactors=k,
                                            beta=consensus_beta,
                                            eta=consensus_eta)
            
            train_norm_counts = load_df_from_npz(self.paths['train_normalized_counts'])
            train_X = sp.coo_matrix(sc.AnnData(train_norm_counts).X)
            train_consensus_HPF.bp, train_consensus_HPF.dp = train_consensus_HPF._get_empirical_hypers(train_X) # assign empirical hyperparameters for the training dataset
            
            (bp, _, xi, _, theta, _, loss) = train_consensus_HPF._fit(train_X, freeze_genes=True)
            train_consensus_HPF.xi = xi
            train_consensus_HPF.theta = theta
    
            topic_labels=np.arange(1,k+1)
    
            # Obtain the consensus GEP and cell usage matrices from the model 
            (W, H) = train_consensus_HPF.cell_score(), train_consensus_HPF.gene_score()
            train_consensus_usages = pd.DataFrame(W, index=train_norm_counts.index, columns=topic_labels)
            train_consensus_spectra = pd.DataFrame(np.transpose(H), columns=train_norm_counts.columns, index=topic_labels)

            rf_pred_norm_counts = train_consensus_usages.dot(train_consensus_spectra)
            frobenius_error = ((train_norm_counts - rf_pred_norm_counts)**2).sum().sum()

        consensus_stats = pd.DataFrame([k, density_threshold, stability, frobenius_error],
                    index = ['k', 'local_density_threshold', 'stability', 'prediction_error'],
                    columns = ['stats'])

        if skip_density_and_return_after_stats:
            return consensus_stats
        
        save_df_to_npz(consensus_spectra, self.paths['consensus_spectra']%(k, density_threshold_repl))
        save_df_to_npz(consensus_usages, self.paths['consensus_usages']%(k, density_threshold_repl))
        save_df_to_npz(consensus_stats, self.paths['consensus_stats']%(k, density_threshold_repl))
        save_df_to_text(consensus_spectra, self.paths['consensus_spectra__txt']%(k, density_threshold_repl))
        save_df_to_text(consensus_usages, self.paths['consensus_usages__txt']%(k, density_threshold_repl))

        # Compute gene-scores for each GEP by regressing usage on Z-scores of TPM
        tpm = load_df_from_npz(self.paths['tpm'])
        tpm_stats = load_df_from_npz(self.paths['tpm_stats'])
        norm_tpm = tpm.subtract(tpm_stats['__mean'], axis=1).div(tpm_stats['__std'], axis=1)
        norm_tpm = norm_tpm.loc[consensus_usages.index, :]
        usage_coef = fast_ols_all_cols_df(consensus_usages, norm_tpm)

        save_df_to_npz(usage_coef, self.paths['gene_spectra_score']%(k, density_threshold_repl))
        save_df_to_text(usage_coef, self.paths['gene_spectra_score__txt']%(k, density_threshold_repl))

        if show_clustering:
            if topics_dist is None:
                topics_dist = squareform(fast_euclidean(l2_spectra.values))
                # (l2_spectra was already filtered using the density filter)
            else:
                # (but the previously computed topics_dist was not!)
                topics_dist = topics_dist[density_filter.values, :][:, density_filter.values]


            spectra_order = []
            for cl in sorted(set(kmeans_cluster_labels)):

                cl_filter = kmeans_cluster_labels==cl


                cl_dist = squareform(topics_dist[cl_filter, :][:, cl_filter])
                cl_dist[cl_dist < 0] = 0 #Rarely get floating point arithmetic issues
                if len(cl_dist) == 0:
                    cl_leaves_order = [0]
                else:
                    cl_link = linkage(cl_dist, 'average')
                    cl_leaves_order = leaves_list(cl_link)

                spectra_order += list(np.where(cl_filter)[0][cl_leaves_order])

            from matplotlib import gridspec
            import matplotlib.pyplot as plt

            # Silhouette plot
        
            fig_sil, ax1 = plt.subplots(1, 1)
            fig_sil.set_size_inches(18, 7)
            ax1.set_xlim([-.1, 1])
            ax1.set_ylim([0, len(l2_spectra.values) + (k + 1) * 10])
            
            sample_silhouette_values = silhouette_samples(l2_spectra.values, kmeans_cluster_labels)
            y_lower = 10
            # recent change here
            for i in range(1,k+1):
                # Aggregate the silhouette scores for samples belonging to
                # cluster i, and sort them
                ith_cluster_silhouette_values = sample_silhouette_values[kmeans_cluster_labels == i]
        
                ith_cluster_silhouette_values.sort()
        
                size_cluster_i = ith_cluster_silhouette_values.shape[0]
                y_upper = y_lower + size_cluster_i
        
                color = cm.nipy_spectral(float(i) / k)
                ax1.fill_betweenx(
                    np.arange(y_lower, y_upper),
                    0,
                    ith_cluster_silhouette_values,
                    facecolor=color,
                    edgecolor=color,
                    alpha=0.7,
                )
        
                # Label the silhouette plots with their cluster numbers at the middle
                ax1.text(-0.05, y_lower + 0.5 * size_cluster_i, str(i))
        
                # Compute the new y_lower for next plot
                y_lower = y_upper + 10  # 10 for the 0 samples
        
            ax1.set_title("The silhouette plot for K factors.")
            ax1.set_xlabel("The silhouette coefficient values")
            ax1.set_ylabel("Factor")
        
            # The vertical line for average silhouette score of all the values
            ax1.axvline(x=stability, color="red", linestyle="--")
        
            ax1.set_yticks([])  # Clear the yaxis labels / ticks
            ax1.set_xticks([-0.1,0,0.2,0.4,0.6,0.8,1])
            
            plt.suptitle(
                "Silhouette analysis for KMeans clustering with %d factors"
                % k,
                fontsize=14,
                fontweight="bold",
            )
            plt.show()
            fig_sil.savefig(self.paths['silhouette_plot']%k, dpi=250)

            # Cluster Plots
        
            width_ratios = [0.5, 9, 0.5, 4, 1]
            height_ratios = [0.5, 9]
            fig = plt.figure(figsize=(sum(width_ratios), sum(height_ratios)))
            gs = gridspec.GridSpec(len(height_ratios), len(width_ratios), fig,
                                    0.01, 0.01, 0.98, 0.98,
                                   height_ratios=height_ratios,
                                   width_ratios=width_ratios,
                                   wspace=0, hspace=0)

            dist_ax = fig.add_subplot(gs[1,1], xscale='linear', yscale='linear',
                                      xticks=[], yticks=[],xlabel='', ylabel='',
                                      frameon=True)

            D = topics_dist[spectra_order, :][:, spectra_order]
            dist_im = dist_ax.imshow(D, interpolation='none', cmap='viridis', aspect='auto',
                                rasterized=True)

            left_ax = fig.add_subplot(gs[1,0], xscale='linear', yscale='linear', xticks=[], yticks=[],
                xlabel='', ylabel='', frameon=True)
            left_ax.imshow(kmeans_cluster_labels.values[spectra_order].reshape(-1, 1),
                            interpolation='none', cmap='Spectral', aspect='auto',
                            rasterized=True)


            top_ax = fig.add_subplot(gs[0,1], xscale='linear', yscale='linear', xticks=[], yticks=[],
                xlabel='', ylabel='', frameon=True)
            top_ax.imshow(kmeans_cluster_labels.values[spectra_order].reshape(1, -1),
                              interpolation='none', cmap='Spectral', aspect='auto',
                                rasterized=True)


            hist_gs = gridspec.GridSpecFromSubplotSpec(3, 1, subplot_spec=gs[1, 3],
                                   wspace=0, hspace=0)

            hist_ax = fig.add_subplot(hist_gs[0,0], xscale='linear', yscale='linear',
                xlabel='', ylabel='', frameon=True, title='Local density histogram')
            hist_ax.hist(local_density.values, bins=np.linspace(0, 1, 50))
            hist_ax.yaxis.tick_right()

            xlim = hist_ax.get_xlim()
            ylim = hist_ax.get_ylim()
            if density_threshold < xlim[1]:
                hist_ax.axvline(density_threshold, linestyle='--', color='k')
                hist_ax.text(density_threshold  + 0.02, ylim[1] * 0.95, 'filtering\nthreshold\n\n', va='top')
            hist_ax.set_xlim(xlim)
            hist_ax.set_xlabel('Mean distance to k nearest neighbors\n\n%d/%d (%.0f%%) spectra above threshold\nwere removed prior to clustering'%(sum(~density_filter), len(density_filter), 100*(~density_filter).mean()))

            fig.savefig(self.paths['clustering_plot']%(k, density_threshold_repl), dpi=250)
            if close_clustergram_fig:
                plt.close(fig)

    def k_selection_plot(self, close_fig=True, train_set=False):
        '''
        Borrowed from Alexandrov Et Al. 2013 Deciphering Mutational Signatures
        publication in Cell Reports
        '''
        run_params = load_df_from_npz(self.paths['nmf_parameters'])
        stats = []
        for k in sorted(set(run_params.n_components)):

            stats.append(self.consensus(k, skip_density_and_return_after_stats=True,train_set=train_set).stats)

        stats = pd.DataFrame(stats)
        stats.reset_index(drop = True, inplace = True)

        save_df_to_npz(stats, self.paths['k_selection_stats'])

        fig = plt.figure(figsize=(6, 4))
        ax1 = fig.add_subplot(111)
        ax2 = ax1.twinx()


        ax1.plot(stats.k, stats.stability, 'o-', color='b')
        ax1.set_ylabel('Silhouette Score', color='b', fontsize=15)
        for tl in ax1.get_yticklabels():
            tl.set_color('b')
        #ax1.set_xlabel('K', fontsize=15)

        ax2.plot(stats.k, stats.prediction_error, 'o-', color='r')
        ax2.set_ylabel('Sum of the Squared Errors', color='r', fontsize=15)
        for tl in ax2.get_yticklabels():
            tl.set_color('r')

        ax1.set_xlabel('Number of Components', fontsize=15)
        ax1.grid('on')
        fig.savefig(self.paths['k_selection_plot'], dpi=250)
        if close_fig:
            plt.close(fig)



if __name__=="__main__":
    """
    Example commands for now:

        output_dir="/Users/averes/Projects/Melton/Notebooks/2018/07-2018/cnmf_test/"


        python cnmf.py prepare --output-dir $output_dir \
           --name test --counts /Users/averes/Projects/Melton/Notebooks/2018/07-2018/cnmf_test/test_data.df.npz \
           -k 6 7 8 9 --n-iter 5

        python cnmf.py factorize  --name test --output-dir $output_dir

        THis can be parallelized as such:

        python cnmf.py factorize  --name test --output-dir $output_dir --total-workers 2 --worker-index WORKER_INDEX (where worker_index starts with 0)

        python cnmf.py combine  --name test --output-dir $output_dir

        python cnmf.py consensus  --name test --output-dir $output_dir

    """

    import sys, argparse
    parser = argparse.ArgumentParser()

    parser.add_argument('command', type=str, choices=['prepare', 'factorize', 'combine', 'consensus', 'k_selection_plot'])
    parser.add_argument('--name', type=str, help='[all] Name for this analysis. All output will be placed in [output-dir]/[name]/...', nargs='?', default=None)
    parser.add_argument('--output-dir', type=str, help='[all] Output directory. All output will be placed in [output-dir]/[name]/...', nargs='?')

    parser.add_argument('-c', '--counts', type=str, help='[prepare] Input counts in cell x gene matrix as df.npz or tab separated txt file')
    parser.add_argument('-k', '--components', type=int, help='[prepare] Numper of components (k) for matrix factorization. Several can be specified with "-k 8 9 10"', nargs='+')
    parser.add_argument('-n', '--n-iter', type=int, help='[prepare] Numper of iteration for each factorization', default=100)

    parser.add_argument('--total-workers', type=int, help='[all] Total workers that are working together.', default=1)
    parser.add_argument('--worker-index', type=int, help='[all] Index of current worker (the first worker should have index 0).', default=0)
    parser.add_argument('--seed', type=int, help='[prepare] Master seed for generating the seed list.', default=None)
    parser.add_argument('--numgenes', type=int, help='[prepare] Number of high variance genes to use for matrix factorization.', default=None)
    parser.add_argument('--genes_file', type=str, help='[prepare] File containing a list of genes to include, one gene per line. Must match column labels of counts matrix.', default=None)
    parser.add_argument('--tpm', type=str, help='[prepare] Pre-computed TPM values as df.npz or tab separated txt file. Cell x Gene matrix. If none is provided, TPM will be calculated automatically. This can be helpful if a particular normalization is desired.', default=None)

    parser.add_argument('--local-density-threshold', type=str, help='[consensus] Threshold for the local density filtering. This string must convert to a greater >0 and <=2. The input value will be replaced', default='0.5')
    parser.add_argument('--local-neighborhood-size', type=float, help='[consensus] Number of nearest neighbors for local density filtering, as a fraction of the total number of iterations.', default=0.30)
    parser.add_argument('--show-clustering', dest='show_clustering', help='[consensus] Produce a clustergram figure summarizing the spectra clustering', action='store_true')
    parser.add_argument('--stats-only', dest='stats_only', help='[consensus] Stop after outputting consistency stats without outputting spectra or usages', action='store_true')


    args = parser.parse_args()
    cnmf_obj = cNMF(output_dir=args.output_dir, name=args.name)
    cnmf_obj._initialize_dirs()

    if args.command == 'prepare':
        if args.counts.endswith('.npz'):
            input_counts = load_df_from_npz(args.counts)
        else:
            input_counts = pd.read_csv(args.counts, sep='\t', index_col=0)

        if args.tpm is not None:
            if args.tpm.endswith('.npz'):
                tpm = load_df_from_npz(args.tpm)
            else:
                tpm = pd.read_csv(args.tpm, sep='\t', index_col=0)
        else:
        	tpm = compute_tpm(input_counts)

        save_df_to_npz(tpm, cnmf_obj.paths['tpm'])
        input_tpm_stats = pd.DataFrame([tpm.mean(axis=0), tpm.std(axis=0)],
             index = ['__mean', '__std']).T
        save_df_to_npz(input_tpm_stats, cnmf_obj.paths['tpm_stats'])


        if args.genes_file is not None:
            highvargenes = open(args.genes_file).read().split('\n')
        else:
            highvargenes = None

        norm_counts = cnmf_obj.get_norm_counts(input_counts, num_highvar_genes=args.numgenes, high_variance_genes_filter=highvargenes)
        cnmf_obj.save_norm_counts(norm_counts)
        run_params = cnmf_obj.get_nmf_iter_params(ks=args.components, n_iter=args.n_iter, random_state_seed=args.seed)
        cnmf_obj.save_nmf_iter_params(run_params)


    elif args.command == 'factorize':
        cnmf_obj.run_nmf(worker_i=args.worker_index, total_workers=args.total_workers)

    elif args.command == 'combine':
        run_params = load_df_from_npz(cnmf_obj.paths['nmf_parameters'])

        if type(args.components) is int:
            ks = [args.components]
        elif args.components is None:
            ks = sorted(set(run_params.n_components))
        else:
            ks = args.components

        for k in ks:
            cnmf_obj.combine_nmf(k)

    elif args.command == 'consensus':
        run_params = load_df_from_npz(cnmf_obj.paths['nmf_parameters'])

        if type(args.components) is int:
            ks = [args.components]
        elif args.components is None:
            ks = sorted(set(run_params.n_components))
        else:
            ks = args.components

        for k in ks:
            merged_spectra = load_df_from_npz(cnmf_obj.paths['merged_spectra']%k)
            cnmf_obj.consensus(k, args.local_density_threshold, args.local_neighborhood_size, args.show_clustering, args.stats_only)

    elif args.command == 'k_selection_plot':
        cnmf_obj.k_selection_plot()
