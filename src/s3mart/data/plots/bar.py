from rpy2.robjects.packages import importr

from s3mart.data.plots.util import save_plot
from s3mart import settings

def plot(*args, **kwargs):
    pseq = importr("phyloseq")
    mothur_data = kwargs["mothur_data"]
    
    pseq_data   = pseq.import_mothur(
        mothur_shared_file  = mothur_data["shared"],
        mothur_list_file    = mothur_data["list"],
        mothur_group_file   = mothur_data["taxonomy"],
        cutoff = settings.get("cutoff_level")
    )
    
    ggplot = pseq.plot_bar(pseq_data)

    save_plot(ggplot, *args, **kwargs)