#!/usr/bin/env python
import os
from itertools import ifilter
import yaml
import subprocess
import sys
import fnmatch
import bz2

def parse_args():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile-data", type=str, default="/eos/cms/store/user/cmsbuild/profiling/data/", help="profiling data location")
    parser.add_argument("--scram-arch", type=str, default="slc7_amd64_gcc900", help="Look for profile files in this scram arch directory")
    parser.add_argument("--release-pattern", type=str, help="Glob string to filter releases that are going to be processed", default="*")
    parser.add_argument("--outfile", type=str, help="output yaml file", default="out.yaml")
    args = parser.parse_args()
    return args

class CallStack:
    def __init__(self, func_data, measurement):
        self.func_data = func_data
        self.measurement = measurement

def cleanStack(stack):
    new_stack = []
    for s in stack:
        if not s in new_stack and len(s)>0:
            new_stack.append(s)
        if s.endswith("doEvent"):
            break
        if s.endswith("beginRun"):
            break
        if s.endswith("edm::Factory::makeModule"):
            break
        if s.endswith("edm::EventProcessor::init"):
            break
    return new_stack

def nameStack(stack):
    if "edm::PoolOutputModule::write" in stack:
        return "PoolOutputModule"
    elif "TBasket::ReadBasketBuffers" in stack or "TBranch::GetEntry" in stack:
        return "InputModule"
    elif "edm::EventPrincipal::clearEventPrincipal" in stack:
        return "edm::EventPrincipal::clearEventPrincipal"
    elif len(stack)>0 and stack[-1].endswith("doEvent"):
        for s in stack:
            if "::produce" in s:
                return s
    elif len(stack)>0 and stack[-1].endswith("beginRun"):
        return stack[-1]
    elif len(stack)>0 and stack[-1].endswith("makeModule"):
        return stack[-5]
    elif len(stack)>0 and stack[-1].endswith("edm::EventProcessor::init"):
        return stack[-1]
    return "other"

def makeIgProfGrouped(infile, outfile): 
    fi = bz2.BZ2File(infile, "rb")
    
    function_stacks = []
    for line in fi.readlines():
    
        #new stack
        if line.startswith("## "):
            stack = []
            stack_measurement = float(line.split()[3][1:].replace("'", ""))
        #line in existing stack
        elif line.startswith("#"):
            line = line.replace("(anonymous namespace)::", "")
            line = line.replace(", ", ",")
            line = line[line.index(" ")+1:]
            line = line.replace(" ", "")
            if "(" in line:
                line = line[:line.index("(")]
                stack.append(line)
        else:
            function_stacks.append(CallStack(stack, stack_measurement))

    ret = {}
    for istack, stack in enumerate(function_stacks):
        new_stack = cleanStack(stack.func_data)
        if len(new_stack) > 0:
            name = nameStack(new_stack)
            if not name in ret:
                ret[name] = 0
            ret[name] += stack.measurement

    with open(outfile, "w") as of:
        for k, v in sorted(ret.items(), key=lambda x: x[1], reverse=True):
            of.write("{};{:.2f}\n".format(k,v))

def getFileSize(fn):
    ret = os.path.getsize(fn)
    return ret

def makeIgProfSummaryMEM(infile, outfile):
    os.system("igprof-analyse --top 1000 --demangle --gdb -r MEM_LIVE {} | bzip2 -9 > {}".format(infile, outfile))
    makeIgProfGrouped(outfile, outfile.replace(".txt.bz2", "_grouped.csv"))
    os.system("igprof-analyse --sqlite -v --demangle --gdb -r MEM_LIVE {} | python fix-igprof-sql.py | sqlite3 {}".format(infile, outfile.replace(".txt.bz2", ".sql3")))

def makeIgProfSummaryCPU(infile, outfile):
    os.system("igprof-analyse --top 1000 --demangle --gdb -r PERF_TICKS {} | bzip2 -9 > {}".format(infile, outfile))
    makeIgProfGrouped(outfile, outfile.replace(".txt.bz2", "_grouped.csv"))
    os.system("igprof-analyse --sqlite -v --demangle --gdb {} | python fix-igprof-sql.py | sqlite3 {}".format(infile, outfile.replace(".txt.bz2", ".sql3")))

def getReleases(dirname):
    ls = os.listdir(dirname)
    ls = [x for x in ls if x.startswith("CMSSW_")]
    return ls

def grep(fn, match):
    ret = []
    with open(fn) as fi:
        for line in fi.readlines():
            if match in line:
                ret.append(line)
    return ret

def getCPUEvent(fn):
    result = grep(fn, "TimeReport       event loop CPU/event =")[0]
    cpu_event = float(result.split("=")[1]) 
    return cpu_event

def getPeakRSS(fn):
    result = grep(fn, "RSS")
    rss_vals = [float(r.split()[7]) for r in result]
    return max(rss_vals)

def parseStep(dirname, release, arch, wf, step):
    base = os.path.join(dirname, release, arch, wf)
    tmi = os.path.join(base, "{}_TimeMemoryInfo.log".format(step))
    rootfile = os.path.join(base, "{}.root.unused".format(step))
    if not os.path.isfile(rootfile):
        rootfile = os.path.join(base, "{}.root".format(step))

    cpu_event = getCPUEvent(tmi)
    peak_rss = getPeakRSS(tmi)
    file_size = getFileSize(rootfile)

    igprof_outpath = "results/igprof/{}/{}/{}".format(release.replace("CMSSW_", ""), wf, step)
    if not os.path.isdir(igprof_outpath):
        os.makedirs(igprof_outpath)
    makeIgProfSummaryCPU(os.path.join(base, "{}_igprofCPU.gz".format(step)), os.path.join(igprof_outpath, "cpu.txt.bz2"))
    makeIgProfSummaryMEM(os.path.join(base, "{}_igprofMEM.gz".format(step)), os.path.join(igprof_outpath, "mem.txt.bz2"))

    return {"cpu_event": cpu_event, "peak_rss": peak_rss, "file_size": file_size}

def getWorkflows(dirname, release, arch):
    ls = os.listdir(os.path.join(dirname, release, arch))
    ls = [x for x in ls if "." in x and int(x.split(".")[0])]
    return ls

def parseRelease(dirname, release, arch):
    print("parsing {} {} {}".format(dirname, release, arch))
    wfs = getWorkflows(dirname, release, arch)
    ret = {}
    for wf in wfs:
        step3_data = parseStep(dirname, release, arch, wf, "step3")
        step4_data = parseStep(dirname, release, arch, wf, "step4")
        
        ret_wf = {}
        for k, v in step3_data.items():
            ret_wf["step3_" + k] = v
        for k, v in step4_data.items():
            ret_wf["step4_" + k] = v
        ret[wf.replace(".", "p")] = ret_wf
    return ret

if __name__ == "__main__":
    args = parse_args()
    releases = getReleases(args.profile_data)

    releases_str = ""
    results = {}
    for release in releases:
        if fnmatch.fnmatch(release, args.release_pattern):
            parsed = parseRelease(args.profile_data, release, args.scram_arch)
            results[release] = parsed
            results[release]["arch"] = args.scram_arch
        else:
            print("skipping {}".format(release))

    with open(args.outfile, "w") as fi:
        fi.write(yaml.dump(results, default_flow_style=False))
