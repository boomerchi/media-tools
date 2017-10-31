"""DASH OnDemand Verifier.

Verify that assets defined by a DASH manifest are good on-demand assets.

Check that

* the manifest uses indexRange and baseURL to specify content.
* sidx durations agree with the actual subsegments
* different representations in the same adaptation set are aligned.

Further restrictions are:
* Text is either TTML or WebVTT as sideloaded files

Return an exit value that is a bitmask combination of

BAD_SIDX = 0x01
BAD_ALIGNMENT = 0x02
BAD_MANIFEST = 0x04
BAD_OTHER = 0x08

A result of 0, means nothing bad found.
"""

import os
import sys
import logging
import traceback
import xml.etree.ElementTree as ET
from argparse import ArgumentParser
from collections import defaultdict, namedtuple, Counter

from mp4 import mp4

log = logging.getLogger('__name__')

BAD_SIDX = 0x01
BAD_ALIGNMENT = 0x02
BAD_MANIFEST = 0x04
BAD_OTHER = 0x08

MAX_NR_VIDEO_ADAPTATION_SETS = 1


def badness_string(badness):
    "Return badness string given value."
    parts = []
    if badness & BAD_SIDX:
        parts.append('sidx mismatch')
    if badness & BAD_ALIGNMENT:
        parts.append('representation misalignment')
    if badness & BAD_MANIFEST:
        parts.append('bad manifest')
    if badness & BAD_OTHER:
        parts.append('other problem')
    return ", ". join(parts)


ns = {'dash': 'urn:mpeg:dash:schema:mpd:2011'}
LOGFILE = 'dashondemand_verifier.log'


TrackDurations = namedtuple('TrackDurations', 'name durations')


class BadManifestError(Exception):
    pass


class CMAFTrack(object):
    "Check and possibly fix a CMAF track."
    def __init__(self, data):
        self.root = mp4(data)
        self.segment_data = self._find_subsegment_data(self.root)
        self.sidx_segment_data = self._get_sidx_segment_data(self.root)

    def _find_subsegment_data(self, mp4_root):
        "Find the segments and return size, offset, decode_time, duration"
        timescale = self.root.find('moov.trak.mdia.mdhd').timescale
        segments = []
        segment = {}
        for top_box in mp4_root.children:
            if not segment and top_box.type in ('emsg', 'styp', 'moof'):
                segment = {'size': 0, 'offset': top_box.offset}
            if segment:
                segment['size'] += top_box.size
                if top_box.type == 'moof':
                    tfdt = top_box.find('traf.tfdt')
                    segment['decode_time'] = tfdt.decode_time
                    trun = top_box.find('traf.trun')
                    segment['duration'] = trun.total_duration
                    if segment['duration'] == 0:  # Must find values in trex
                        trex = self.root.find('mvex.trex')
                        segment['duration'] = (trex.default_sample_duration *
                                               trun.sample_count)
                    if len(segments) > 0:
                        last_seg = segments[-1]
                        self._check_duration_consistency(segment, last_seg)

                elif top_box.type == 'mdat':
                    segments.append(segment)
                    segment = {}
        segment_data = {'timescale': timescale,
                        'segments': segments}
        return segment_data

    def _check_duration_consistency(self, segment, last_seg):
        "Check that last_seg ends at the time segment starts."
        last_end = last_seg['decode_time'] + last_seg['duration']
        if segment['decode_time'] != last_end:
            log.error("Segment end %d not equal to next segment start %d" %
                      (last_end, segment['decode_time']))

    def _get_sidx_segment_data(self, mp4_root):
        "Return sidx segment data."
        sidx = mp4_root.find('sidx')
        if not sidx:
            return []
        offset = sidx.size + sidx.offset + sidx.first_offset
        pres_time = sidx.first_pres_time
        sidx_segments = []
        for ref in sidx.references:
            size = ref['referenced-size']
            duration = ref['subsegment-duration']
            sidx_segments.append({'size': size, 'offset': offset,
                                  'decode_time': pres_time,
                                  'duration': duration})
            offset += size
            pres_time += duration
        sidx_data = {'timescale': sidx.timescale,
                     'segments': sidx_segments}
        return sidx_data


def get_media_type(rep, adaptation_set):
    "Get mediatype for representation and adaptation set."
    mime_type = rep.attrib.get('mimeType')
    if not mime_type:
        mime_type = adaptation_set.attrib.get('mimeType')
    if not mime_type:
        raise BadManifestError("Representation id=%s lacks mime type ",
                               rep.attrib['id'])
    if mime_type.startswith('video'):
        media_type = "video"
    elif mime_type.startswith('audio'):
        media_type = "audio"
    elif mime_type == 'text/vtt':  # Side-loaded WebVTT file
        media_type = "text"
    elif mime_type == 'application/ttml+xml':  # Side-loaded TTML file
        media_type = "text"
    if mime_type == 'application/mp4':
        raise BadManifestError("Mime type %s not supported. Use side-loaded "
                               "text files for subtitles." % mime_type)
    return media_type


def check_dash_manifest(manifest_path, verbose):
    """Check that DASH manifest is OnDemand with side-loaded subtitles."""
    tree = ET.parse(manifest_path)
    root = tree.getroot()
    if root.attrib['type'] != 'static':
        raise BadManifestError("MPD type is not static")
    all_periods = root.findall("dash:Period", ns)
    if len(all_periods) != 1:
        raise BadManifestError("Only exactly one period supported id")
    all_as = root.findall("dash:Period/dash:AdaptationSet", ns)
    for adaptation_set in all_as:
        reps = adaptation_set.findall('dash:Representation', ns)
        for rep in reps:
            if not rep.attrib.get('id'):
                raise BadManifestError("Representation does not have id "
                                       "attribute")
            base_url = rep.find('dash:BaseURL', ns)
            if base_url is None:
                raise BadManifestError("Representation id=%s does not have "
                                       "BaseURL", rep.attrib['id'])
            media_type = get_media_type(rep, adaptation_set)
            if media_type in ('video', 'audio', 'text'):
                seg_base = rep.find('dash:SegmentBase', ns)
                if seg_base is None:
                    raise BadManifestError("Representation id=%s does not "
                                           "have SegmentBase",
                                           rep.attrib['id'])
            if media_type in ('video', 'audio'):
                if "indexRange" not in seg_base.attrib:
                    raise BadManifestError("Representation id=%s does not "
                                           "have SegmentBase.indexRange",
                                           rep.attrib['id'])


def get_trackgroups_from_dash_manifest(manifest_path):
    "Get track_paths grouped by adapation sets"
    track_file_paths = []
    mpd_baseurl = os.path.dirname(manifest_path)
    tree = ET.parse(manifest_path)
    root = tree.getroot()
    top_baseurl = root.find("dash:BaseURL", ns)
    if top_baseurl is not None:
        mpd_baseurl = os.path.join(mpd_baseurl, top_baseurl.text)
    all_as = root.findall("dash:Period/dash:AdaptationSet", ns)
    nr_video_as = 0
    for adaptation_set in all_as:
        as_media_type = None
        as_baseurl = mpd_baseurl
        asbu = root.find("dash:BaseURL", ns)
        if asbu is not None:
            as_baseurl = os.path.join(as_baseurl, asbu.text)
        track_file_paths.append([])
        reps = adaptation_set.findall('dash:Representation', ns)
        for rep in reps:
            if as_media_type is None:
                as_media_type = get_media_type(rep, adaptation_set)
                nr_video_as += 1
            path = rep.find('dash:BaseURL', ns).text
            total_path = os.path.join(as_baseurl, path)
            track_file_paths[-1].append(total_path)
    if nr_video_as > MAX_NR_VIDEO_ADAPTATION_SETS:
        log.warning('%d video adaptation sets. Only %d supported' %
                    (nr_video_as, MAX_NR_VIDEO_ADAPTATION_SETS))
    return track_file_paths


def check_alignment(manifest_path, verbose):
    """Check alignment and return badness as mask.

    0 is OK. 1 is sidx issue. 2 is inter-track-alignment issue."""
    badness = 0
    track_groups = get_trackgroups_from_dash_manifest(manifest_path)
    for nr, track_group in enumerate(track_groups):
        log.info("Checking adaptation set group nr %d (%d files)" %
                 ((nr + 1), len(track_group)))
        log.info(", ".join(os.path.basename(t) for t in track_group))
        track_durations = []  # Tuples of (name, segments)
        segment_timescale = None
        sidx_timescale = None
        for i, track_path in enumerate(track_group):
            name = os.path.basename(track_path)
            data = open(track_path, 'rb').read()
            track = CMAFTrack(data)
            this_seg_timescale = track.segment_data['timescale']
            this_sidx_timescale = track.sidx_segment_data['timescale']
            if segment_timescale is None:
                segment_timescale = this_seg_timescale
            else:
                if this_seg_timescale != segment_timescale:
                    raise ValueError("New track timescale %d (not %d) for %s" %
                                     this_seg_timescale, segment_timescale,
                                     name)
            if sidx_timescale is None:
                sidx_timescale = this_sidx_timescale
            else:
                if this_sidx_timescale != sidx_timescale:
                    raise ValueError("New track timescale %d (not %d) for %s" %
                                     this_sidx_timescale, sidx_timescale,
                                     name)
            log.info("%3d name=%s timescale=%d sidx_timescale=%d" %
                     (i + 1, track_path, segment_timescale, sidx_timescale))
            if not compare_segments_and_sidx(name, track):
                log.error("SIDX/Segment mismatch in %s" % track_path)
                badness |= BAD_SIDX
            track_durs = [t['duration'] for t in
                          track.segment_data['segments']]
            track_durations.append(TrackDurations(track_path, track_durs))
    nr_bad_tracks = check_track_group_alignment(track_durations)
    if nr_bad_tracks > 0:
        badness |= BAD_ALIGNMENT
    return badness


def compare_segments_and_sidx(track_name, track):
    "Check that sidx is compatible with segment data."
    seg_data = track.segment_data
    sidx_data = track.sidx_segment_data
    seg_timescale = seg_data['timescale']
    sidx_timescale = sidx_data['timescale']
    equal = True
    if len(sidx_data['segments']) != len(seg_data['segments']):
        log.error("Sidx has %d segments, while there are %d" %
                  (len(sidx_data['segments']), len(seg_data['segments'])))
        return False
    for i, (sidx_seg, seg_seg) in enumerate(zip(sidx_data['segments'],
                                                seg_data['segments'])):
        if (sidx_seg['duration'] * seg_timescale != seg_seg['duration'] *
            sidx_timescale):
            log.debug("Sidx duration mismatch for segment %d (%d, %d) != ("
                      "%d %d)" % ((i + 1), sidx_seg['duration'],
                                  sidx_timescale, seg_seg['duration'],
                                  seg_timescale))
            equal = False
    return equal


def check_track_group_alignment(track_durations):
    "Check if all tracks in group are aligned."
    if len(track_durations) == 1:
        return 0
    nr_mismatches = defaultdict(int)
    for i in range(len(track_durations) - 1):
        for j in range(i + 1, len(track_durations)):
            if track_durations[i].durations != track_durations[j].durations:
                name1 = track_durations[i].name
                name2 = track_durations[j].name
                diffs = []
                for dur1, dur2 in zip(track_durations[i].durations,
                                      track_durations[j].durations):
                    diffs.append(dur1 - dur2)
                log.info("Diffs between %s and %s: %s" % (name1, name2,
                                                          Counter(diffs)))
                nr_mismatches[name1] += 1
                nr_mismatches[name2] += 1
    nr_bad_tracks = 0
    if len(track_durations) > 2:
        for name, mismatches in nr_mismatches.iteritems():
            if mismatches > 1:
                log.error("Track %s is not aligned with %d other tracks" %
                          (name, mismatches))
                nr_bad_tracks += 1
    elif len(track_durations) == 2:
        if nr_mismatches[track_durations[0]] > 0:
            nr_bad_tracks = 1
    return nr_bad_tracks


def setup_logging(log_level, log_to_stdout):
    log_level = log_level.upper()

    # default loglevel is warning
    numeric_level = getattr(logging, log_level, logging.WARNING)
    logger = logging.getLogger()
    logger.setLevel(numeric_level)
    if log_to_stdout:
        log_handler = logging.StreamHandler()
    else:
        print("dashondemand_verifier: Logging to %s" % LOGFILE)
        if os.path.exists(LOGFILE):
            os.remove(LOGFILE)
        log_handler = logging.FileHandler(LOGFILE)
    formatter = logging.Formatter('%(name)s - %(levelname)s - %(message)s')
    log_handler.setFormatter(formatter)
    logger.addHandler(log_handler)


def check_asset(mpd_path, verbose):
    "Check a file."
    print "Checking %s" % mpd_path
    log.info("Checking %s" % mpd_path)
    badness = 0
    try:
        try:
            check_dash_manifest(mpd_path, verbose)
        except BadManifestError, e:
            badness = BAD_MANIFEST
            log.error(e.message)
            if verbose:
                print(e.message)
                traceback.print_tb(sys.exc_traceback)
        else:
            badness  |= check_alignment(mpd_path, verbose)
    except Exception, e:
        log.error(e.message)
        if verbose:
            print(e.message)
            traceback.print_tb(sys.exc_traceback)
        badness |= BAD_OTHER
    if badness != 0:
        print "Asset %s has badness %d: %s" % (mpd_path, badness,
                                               badness_string(badness))
    else:
        print "Asset %s is OK" % mpd_path
    return badness

def check_asset_tree(verbose, asset_dir, names):
    badness = 0
    for name in names:
        path = os.path.join(asset_dir, name)
        base, ext = os.path.splitext(path)
        if ext == '.mpd':
            badness |= check_asset(path, verbose)



def cli():
    parser = ArgumentParser(usage="usage: %(prog)s [options]")

    parser.add_argument("manifest_files", nargs="*")

    parser.add_argument("--stdout",
                        action="store_true",
                        dest="log_to_stdout",
                        help="Log to stdout instead of file")

    parser.add_argument("-l", "--log-level",
                        dest="log_level",
                        default="WARNING",
                        help="One of "
                             "INFO|DEBUG|WARNING (default)|ERROR|CRITICAL")

    parser.add_argument("-v", "--verbose",
                        action="store_true",
                        dest="verbose")

    args = parser.parse_args()
    setup_logging(args.log_level, args.log_to_stdout)
    badness = 0
    for asset_path in args.manifest_files:
        if os.path.isdir(asset_path):
            print("Traversing tree looking for mpd files at %s" % asset_path)
            os.path.walk(asset_path, check_asset_tree, args.verbose)
        else:
            asset_badness = check_asset(asset_path, args.verbose)
            badness |= asset_badness
    sys.exit(badness)


if __name__ == "__main__":
    cli()
