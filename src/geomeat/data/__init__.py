import os.path as osp
import csv

import tqdm as tq

from geomeat.config  import PATH
from geomeat import settings, __name__ as NAME
from geomeat.data.util import install_silva

from bpyutils.util.ml      import get_data_dir
from bpyutils.util.array   import chunkify
from bpyutils.util._dict   import dict_from_list, merge_dict
from bpyutils.util.types   import lmap, auto_typecast, build_fn
from bpyutils.util.system  import (
    ShellEnvironment,
    makedirs,
    make_temp_dir, get_files, copy, write, move
)
from bpyutils.util.string    import get_random_str
from bpyutils.exception      import PopenError
from bpyutils._compat import itervalues
from bpyutils import parallel, log

from geomeat.data.util import render_template

logger = log.get_logger(name = NAME)

CACHE  = PATH["CACHE"]

def get_csv_data(sample = False):
    path_data = osp.join(PATH["DATA"], "sample.csv" if sample else "data.csv")
    data      = []
    
    with open(path_data) as f:
        reader = csv.reader(f)
        header = next(reader, None)

        data = lmap(lambda x: dict_from_list(header, lmap(auto_typecast, x)), reader)

    return data

def get_fastq(meta, data_dir = None, *args, **kwargs):
    sra, layout = meta["sra"], meta["layout"]

    jobs     = kwargs.get("jobs", settings.get("jobs"))
    data_dir = get_data_dir(NAME, data_dir)

    with ShellEnvironment(cwd = data_dir) as shell:
        sra_dir = osp.join(data_dir, sra)

        logger.info("Checking if SRA %s is prefetched..." % sra)
        path_sra = osp.join(sra_dir, "%s.sra" % sra)

        if not osp.exists(path_sra):
            logger.info("Performing prefetch for SRA %s in directory %s." % (sra, sra_dir))
            code = shell("prefetch -O {output_dir} {sra}".format(output_dir = sra_dir, sra = sra))

            if not code:
                logger.success("Successfully prefeteched SRA %s." % sra)

                logger.info("Validating SRA %s..." % sra)
                logger.info("Performing vdb-validate for SRA %s in directory %s." % (sra, sra_dir))
                code = shell("vdb-validate {dir}".format(dir = sra_dir))

                if not code:
                    logger.success("Successfully validated SRA %s." % sra)
                else:
                    logger.error("Unable to validate SRA %s." % sra)
                    return
            else:
                logger.error("Unable to prefetech SRA %s." % sra)
                return
        else:
            logger.warn("SRA %s already prefeteched." % sra)

        logger.info("Checking if FASTQ files for SRA %s has been downloaded..." % sra)
        fastq_files = get_files(sra_dir, "*.fastq")
        
        if not fastq_files:
            logger.info("Downloading FASTQ file(s) for SRA %s..." % sra)
            args = "--split-files" if layout == "paired" else "" 
            code = shell("fasterq-dump --threads {threads} {args} {sra}".format(
                threads = jobs, args = args, sra = sra), cwd = sra_dir)

            if not code:
                logger.success("Successfully downloaded FASTQ file(s) for SRA %s." % sra)
            else:
                logger.error("Unable to download FASTQ file(s) for SRA %s." % sra)
        else:
            logger.warn("FASTQ file(s) for SRA %s already exist." % sra)

def get_data(data_dir = None, check = False, *args, **kwargs):
    jobs = kwargs.get("jobs", settings.get("jobs"))

    data_dir = get_data_dir(NAME, data_dir)
    logger.info("Created data directory at %s." % data_dir)

    data = get_csv_data(sample = check)

    logger.info("Fetching FASTQ files...")
    with parallel.no_daemon_pool(processes = jobs) as pool:
        length   = len(data)

        function = build_fn(get_fastq, data_dir = data_dir,
            raise_err = False, *args, **kwargs)
        results  = pool.imap(function, data)

        list(tq.tqdm(results, total = length))

def _get_fastq_file_line(fname):
    prefix, _ = osp.splitext(fname)
    prefix    = osp.basename(prefix)

    return "%s %s" % (prefix, fname)

def _build_mothur_script(template, output, config):
    logger.info("Building script %s for mothur." % template)

    mothur_script = render_template(template = template, **config)
    write(output, mothur_script)

def _mothur_filter_files(config, data_dir = None, *args, **kwargs):
    logger.info("Using config %s to filter files." % config)

    jobs       = kwargs.get("jobs", settings.get("jobs"))
    data_dir   = get_data_dir(NAME, data_dir)

    files      = config.pop("files")
    target_dir = config.pop("target_dir")

    primer_f   = config.pop("primer_f")
    primer_r   = config.pop("primer_r")

    layout     = config.get("layout")
    trim_type  = config.get("trim_type")

    sra_id     = config.pop("sra_id")

    target_types = ("fasta", "group", "summary")
    target_path  = dict_from_list(
        target_types,
        lmap(lambda x: osp.join(target_dir, "filtered.%s" % x), target_types)
    )

    if not all(osp.exists(x) for x in itervalues(target_path)):
        with make_temp_dir(root_dir = CACHE) as tmp_dir:
            logger.info("[SRA %s] Copying FASTQ files %s for pre-processing at %s." % (sra_id, files, tmp_dir))
            copy(*files, dest = tmp_dir)

            prefix = get_random_str()
            logger.info("[SRA %s] Using prefix for mothur: %s" % (sra_id, prefix))

            logger.info("[SRA %s] Setting up directory %s for preprocessing" % (sra_id, tmp_dir))

            if layout == "single":
                fastq_file = osp.join(tmp_dir, "%s.file" % prefix)
                fastq_data = "\n".join(lmap(_get_fastq_file_line, files))
                write(fastq_file, fastq_data)

                config["fastq_file"] = fastq_file

                config["group"] = osp.join(tmp_dir, "%s.group" % prefix)

            if layout == "paired" and trim_type == "false":
                oligos_file = osp.join(tmp_dir, "primers.oligos")
                oligos_data = "primer %s %s" % (primer_f, primer_r)
                write(oligos_file, oligos_data)

                config["oligos"] = oligos_file

            mothur_file = osp.join(tmp_dir, "script")
            _build_mothur_script("mothur/filter", 
                output = mothur_file,
                config = dict(
                    inputdir = tmp_dir, prefix = prefix, processors = jobs,
                    qaverage = settings.get("quality_average"),
                    maxambig = settings.get("maximum_ambiguity"),
                    maxhomop = settings.get("maximum_homopolymers")
                )
            )

            logger.info("[SRA %s] Running mothur..." % sra_id)

            try:
                with ShellEnvironment(cwd = tmp_dir) as shell:
                    code = shell("mothur %s" % mothur_file)

                    if not code:
                        logger.success("[SRA %s] mothur ran successfully." % sra_id)

                        logger.info("[SRA %s] Attempting to copy filtered files." % sra_id)

                        choice = (
                            ".trim.contigs.trim.good.fasta",
                            ".contigs.good.groups",
                            ".trim.contigs.trim.good.summary"
                        ) if layout == "paired" else (
                            ".trim.good.fasta",
                            ".good.group",
                            ".trim.good.summary"
                        )
                            # group(s): are you f'king kiddin' me?

                        makedirs(target_dir, exist_ok = True)
                
                        copy(
                            osp.join(tmp_dir, "%s%s" % (prefix, choice[0])),
                            dest = target_path["fasta"]
                        )

                        copy(
                            osp.join(tmp_dir, "%s%s" % (prefix, choice[1])),
                            dest = target_path["group"]
                        )

                        copy(
                            osp.join(tmp_dir, "%s%s" % (prefix, choice[2])),
                            dest = target_path["summary"]
                        )

                        logger.info("[SRA %s] Successfully copied filtered files at %s." % (sra_id, target_dir))
            except PopenError as e:
                logger.error("[SRA %s] Unable to filter files. Error: %s" % (sra_id, e))
    else:
        logger.warn("[SRA %s] Filtered files already exists." % sra_id)

def merge_fastq(data_dir = None):
    data_dir = get_data_dir(NAME, data_dir = data_dir)

    logger.info("Finding files in directory: %s" % data_dir)
    
    filtered = get_files(data_dir, "filtered.fasta")
    groups   = get_files(data_dir, "filtered.group")

    if filtered and groups:
        logger.info("Merging %s filter and %s group files." % (len(filtered), len(groups)))

        output_fasta = osp.join(data_dir, "merged.fasta")
        output_group = osp.join(data_dir, "merged.group")

        with make_temp_dir(root_dir = CACHE) as tmp_dir:
            mothur_file = osp.join(tmp_dir, "script")
            _build_mothur_script("mothur/preprocess", 
                output = mothur_file,
                config = dict(
                    input_fastas = filtered,
                    input_groups = groups,
                    output_fasta = output_fasta,
                    output_group = output_group
                )
            )

            with ShellEnvironment(cwd = tmp_dir) as shell:
                code = shell("mothur %s" % mothur_file)

                if not code:
                    # HACK: weird hack around failure of mothur detecting output for merge.files
                    merged_fasta = get_files(data_dir, "merged.fasta")
                    merged_group = get_files(data_dir, "merged.group")

                    move(*merged_fasta, dest = output_fasta)
                    move(*merged_group, dest = output_group)

                    logger.success("Successfully merged.")
                else:
                    logger.error("Error merging files.")
    else:
        logger.warn("No files found to merge.")

def filter_fastq(data_dir = None, check = False, *args, **kwargs):
    jobs = kwargs.get("jobs", settings.get("jobs"))    

    data_dir = get_data_dir(NAME, data_dir = data_dir)

    data = get_csv_data(sample = check)

    mothur_configs = [ ]

    logger.info("Building configs for mothur...")

    for d in data:
        sra_id  = d["sra"]
        sra_dir = osp.join(data_dir, sra_id)
        fastq_files = get_files(sra_dir, type_ = "*.fastq")

        mothur_configs.append({
            "files": fastq_files,
            "target_dir": sra_dir,

            "sra_id": sra_id,

            "primer_f": d["primer_f"],
            "primer_r": d["primer_r"],

            "layout": d["layout"], "trim_type": d["trimmed"],
            
            "min_length": d["min_length"],
            "max_length": d["max_length"]
        })

    if mothur_configs:
        logger.info("Filtering files using mothur using %s jobs...." % jobs)

        filter_chunks = settings.get("filter_chunks")

        for chunk in chunkify(mothur_configs, filter_chunks):
            with parallel.no_daemon_pool(processes = jobs) as pool:
                length    = len(mothur_configs)
                function_ = build_fn(_mothur_filter_files, *args, **kwargs)
                results   = pool.imap(function_, chunk)

                list(tq.tqdm(results, total = length))

def preprocess_fasta(data_dir = None):
    data_dir = get_data_dir(NAME, data_dir)

    merged_fasta = osp.join(data_dir, "merged.fasta")
    merged_group = osp.join(data_dir, "merged.group")

    with make_temp_dir(root_dir = CACHE) as tmp_dir:
        mothur_file = osp.join(tmp_dir, "script")
        _build_mothur_script("mothur/preprocess", 
            output = mothur_file,
            config = dict(
                merged_fasta = merged_fasta,
                merged_group = merged_group
            )
        )

        with ShellEnvironment(cwd = tmp_dir) as shell:
            code = shell("mothur %s" % mothur_file)

            if not code:
                pass
            else:
                logger.error("Error merging files.")

def preprocess_data(data_dir = None, check = False, *args, **kwargs):
    data_dir = get_data_dir(NAME, data_dir)

    logger.info("Attempting to filter FASTQ files...")
    filter_fastq(data_dir = data_dir, check = check, *args, **kwargs)

    logger.info("Merging FASTQs...")
    merge_fastq(data_dir = data_dir)

    logger.info("Installing SILVA...")
    install_silva()

    logger.info("Pre-processing FASTA + Group files...")
    preprocess_fasta(data_dir = data_dir)

def check_data(data_dir = None):
    pass