"""encode video with CLIP"""
import sys

import open_clip
import math
import numpy as np
import torch

from torchvision.transforms import ToPILImage
from video2numpy.frame_reader import FrameReader

from .reader import Reader
from .simplemapper import FrameMapper
from .utils import block2dl
from .writer import FileWriter, WebDatasetWriter
from .distributed import world_info_from_env


BATCH_SIZE = 256
EMB_DIM = 512
N_DATASET_WORKERS = 6
CHUNK_SIZE = 200


def _convert_image_to_rgb(image):
    return image.convert("RGB")


def encode_chunk(frames, ind_dict, writer, mapper, preprocess, meta, ids, use_dst_name, device):
    """encodes a chunk of video frames and saves."""
    vid_block = np.concatenate(frames)
    dl = block2dl(vid_block, preprocess, BATCH_SIZE, N_DATASET_WORKERS)

    embeddings = []
    for batch in dl:
        with torch.no_grad(), torch.cuda.amp.autocast():
            emb = mapper(batch.to(device))
            embeddings.append(emb)

    embeddings = np.concatenate(embeddings)
    for ref, (i0, it, dst_name) in ind_dict.items():
        vid_id = dst_name[:-4] if use_dst_name else ids[ref]
        vid_meta = {}
        for k in meta:
            vid_meta[k] = meta[k][ref].as_py()
        writer.write(embeddings[i0:it], vid_id, vid_meta)


def clip_video_encode(
    src,
    dest="",
    output_format="files",
    take_every_nth=1,
    frame_workers=1,
    frame_memory_size=4,
    metadata_columns="",
    use_dst_name=False,
    distribute="none",
    oc_model_name="ViT-B-32",
    pretrained="laion2b_s34b_b79k",
    imag_size=224
):
    """
    Encode frames using CLIP image encoder

    Input:
      src:
        str: path to mp4 file
        str: youtube link
        str: path to txt file with multiple mp4's or youtube links
        list: list with multiple mp4's or youtube links
      dest:
        str: directory where to save embeddings to
        None: dest = src + .npy
      output_format:
        str: "files" or "webdataset"
      take_every_nth:
        int: only take every nth frame
      frame_workers:
        int: number of Processes to distribute video reading to.
      frame_memory_size:
        int: GB of memory for FrameReader.
      metadata_columns:
        str: a comma separated list of metadata column names to look for in src
      use_dst_name:
        bool: use the save name suggested by video2numpy
      distribute:
        str: distribution strategy, currently either slurm or none
      oc_model_name:
        str: open_clip model name, used for selecting CLIP architecture
      pretrained:
        str: open_clip pretrained weights name
    """
    if isinstance(metadata_columns, str):
        metadata_columns = [metadata_columns] if metadata_columns != "" else []
    metadata_columns = list(metadata_columns) if isinstance(metadata_columns, tuple) else metadata_columns
    reader = Reader(src, metadata_columns)
    vids, ids, meta = reader.get_data()
    meta_refs = list(range(len(vids)))

    starting_shard_id = 0
    shard_sample_count = 10000

    if distribute == "slurm":
        local_rank, global_rank, world_size = world_info_from_env()
        work_size = math.ceil(len(vids) / world_size)
        print(f"Slurm worker {global_rank} processing {work_size} videos...")
        ws, wf = global_rank * work_size, (global_rank + 1) * work_size
        vids = vids[ws:wf]
        ids = ids[ws:wf]
        for mc in meta.keys():
            meta[mc] = meta[mc][ws:wf]
        starting_shard_id += math.ceil(work_size / shard_sample_count) * global_rank
        device = f"cuda:{local_rank}" if torch.cuda.is_available() else "cpu"
    else:
        device = "cuda" if torch.cuda.is_available() else "cpu"

    assert output_format in ["files", "webdataset"]
    if output_format == "files":
        writer = FileWriter(dest)
    elif output_format == "webdataset":
        # TODO: maybe include params for this?
        writer = WebDatasetWriter(dest, 9, "npy", maxcount=shard_sample_count, shard_id=starting_shard_id)

    # Initialize model:
    model, _, preprocess = open_clip.create_model_and_transforms(oc_model_name, pretrained=pretrained, device=device)
    preprocess.transforms = [ToPILImage()] + preprocess.transforms[-3:]

    fm = FrameMapper(model, device)
    fr = FrameReader(vids, meta_refs, take_every_nth, imag_size, workers=frame_workers, memory_size=frame_memory_size)
    fr.start_reading()

    frames, ind_dict = [], {}
    block_size = 0
    i = 0
    for vid_frames, info in fr:
        i += 1
        frames.append(vid_frames)
        ind_dict[info["reference"]] = (block_size, block_size + vid_frames.shape[0], info["dst_name"])
        block_size += vid_frames.shape[0]

        if i % CHUNK_SIZE == 0:
            encode_chunk(frames, ind_dict, writer, fm, preprocess, meta, ids, use_dst_name, device)
            frames, ind_dict, block_size = [], {}, 0

    if len(frames) > 0:  # TODO: make this cleaner
        encode_chunk(frames, ind_dict, writer, fm, preprocess, meta, ids, use_dst_name, device)


if __name__ == "__main__":
    if len(sys.argv) != 4:
        print("Usage: python clip-video-encode.py video.mp4 embeddings.npy take_every_nth")
        sys.exit(1)

    clip_video_encode(sys.argv[1], sys.argv[2], int(sys.argv[3]))
