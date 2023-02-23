import sys
import argparse

#sys.path.insert(0, r"/home/stcadmin/work/onnxruntime/build/py38/Release/build/lib")

import onnxruntime
from pathlib import Path
import shutil
from tabulate import tabulate

sys.path.append(str(Path(__file__).parent.parent.resolve()))
from data.get_test_data import (
    get_backbone_onnx_path,
    get_tokenizer_and_huggingface_model,
)

import transformers
import numpy as np
import onnx
import onnx.numpy_helper
import ort_aot
import ort_aot.logger

import bench_on_android
logger = ort_aot.logger.logger


def verify_results(output_path: Path, model_name: str, onnx_bert_model: Path = None):
    """
    Args:
        output_path: the onnx model which finalized and needs to be verified
        model_name: the huggingface model name
        onnx_bert_model: the onnx model which is generated by huggingface or user provide
    """
    tokenizer, hg_model, _, text = get_tokenizer_and_huggingface_model(model_name)
    encoded_input = tokenizer(*text, return_tensors="pt")

    # save inputs data for debug
    test_data_dir = output_path.parent / "test_data"
    shutil.rmtree(str(test_data_dir), ignore_errors=True)
    test_data_dir.mkdir(exist_ok=True)

    if model_name == 'microsoft/deberta-base':
        encoded_input.pop('token_type_ids')
    for idx, (k, v) in enumerate(encoded_input.items()):
        input_tensor = onnx.numpy_helper.from_array(v.numpy(), name=k)
        open(f"{test_data_dir}/input_{idx}.pb", "wb").write(
            input_tensor.SerializeToString()
        )

    transformers.set_seed(42)

    aot_model = onnx.load(str(output_path.resolve(strict=True)))
    aot_model_output = {i.name: i for i in aot_model.graph.output}
    del aot_model

    session_options = onnxruntime.SessionOptions()
    if onnx_bert_model.exists():
        onnx_model = onnx.load(str(onnx_bert_model.resolve(strict=True)))
        for i in onnx_model.graph.output:
            aot_model_output.pop(i.name)
        onnx_model.graph.output.extend(aot_model_output.values())
        session = onnxruntime.InferenceSession(
            onnx_model.SerializePartialToString(),
            #str(onnx_bert_model.resolve(strict=True)),
            providers=["CPUExecutionProvider"],
        )
        del onnx_model
        inputs = {key: value.detach().numpy() for key, value in encoded_input.items()}

        ref_outputs = session.run([i.name for i in session.get_outputs()], inputs)
        ref_map_out = {
            i.name: ref_outputs[idx] for idx, i in enumerate(session.get_outputs())
        }
    else:
        outs = hg_model(**encoded_input)
        ref_outputs = [out.detach().numpy() for out in list(outs.values())]
        ref_map_out = {i: ref_outputs[idx] for idx, i in enumerate(outs.keys())}

    session = onnxruntime.InferenceSession(
        str(output_path.resolve(strict=True)),
        session_options,
        providers=["CPUExecutionProvider"],
    )

    real_outputs = session.run([i.name for i in session.get_outputs()], inputs)
    matched_idx = [
        i
        for i, o in enumerate(session.get_outputs())
        if list(ref_map_out.keys())[0] in o.name
    ][0]

    assert np.allclose(
        real_outputs[matched_idx],
        ref_outputs[0],
        atol=1e-2,
        rtol=1e-3,
    ), f"Results do not match, expected:{ref_outputs[0]}, \nbut got {real_outputs[matched_idx] }, \ndiff:{real_outputs[matched_idx] - ref_outputs[0]}"
    logger.info(
        f"Results matches:{real_outputs[0]},\ndiff: {real_outputs[matched_idx] - ref_outputs[0]}"
    )

class BenchMarkData(object):
    def __init__(self, model_name: str, ort_time: float, aot_time: float, reduce_nodes: int):
        self.model_name = model_name
        self.ort_time = ort_time
        self.aot_time = aot_time
        self.reduce_nodes = reduce_nodes

class Statistic(object):
    __init_flag = False
    _instance = None
    def __new__(cls, *args, **kwargs):
        if cls._instance:
            return cls._instance
        else:
            cls._instance = super().__new__(cls)
        return cls._instance
        
    def __init__(self, data: BenchMarkData = None):
        if self.__init_flag:
            if data is not None:
                self.datas.append(data)
            return
        self.__init_flag = True
        self.datas = [data]

class BenchmarkContext(object):
    __init_flag = False
    _instance = None
    def __new__(cls, *args, **kwargs):
        if cls._instance:
            return cls._instance
        else:
            cls._instance = super().__new__(cls)
        return cls._instance
        
    def __init__(self, data: BenchMarkData = None):
        if self.__init_flag == False:
            self.__init_flag = True
            self.target = "x86_64"
            self.device = "cpu"

def do_benchmark(output_model_path: Path, input_model_path: Path, model_name: str) -> list:
    model_name=model_name.replace('_dg', '')
    tokenizer, hg_model, _, text = get_tokenizer_and_huggingface_model(model_name)
    encoded_input = tokenizer(*text, return_tensors="np")

    if model_name == 'microsoft/deberta-base':
        encoded_input.pop('token_type_ids')
        
    session_options = onnxruntime.SessionOptions()
    # session_options.intra_op_num_threads = 4
    # session_options.enable_profiling = True
    # session_options.optimized_model_filepath = "./o3.onnx"
    session_options.log_severity_level = 4

    session_in = onnxruntime.InferenceSession(
        str(input_model_path.resolve(strict=True)),
        session_options,
        providers=["CPUExecutionProvider"],
    )
    session_options.optimized_model_filepath = ""

    session_out = onnxruntime.InferenceSession(
        str(output_model_path.resolve(strict=True)),
        session_options,
        providers=["CPUExecutionProvider"],
    )
    inputs = dict(encoded_input)

    c_tc = []
    # warmup
    def do_bench(session, warmup=10, repeat=100, c_tc=c_tc):
        for _ in range(warmup):
            _ = session.run(None, inputs)

        with ort_aot.CostTime(c_tc, repeat) as tc:
            for i in range(repeat):
                _ = session.run(None, inputs)
        prof_file = None  # session.end_profiling()
        return prof_file

    do_bench(session_in)
    do_bench(session_out)
    
    print(
        #f"time-cost changes from {c_tc[0]:.6}ms to {c_tc[1]:.6}ms, speedup: {(c_tc[0]-c_tc[1])*100/c_tc[0]:.2f}%"
        f"time-cost changes from {c_tc[0]:.6}ms to {c_tc[1]:.6}ms, speedup: {(c_tc[0]/c_tc[1]):.2f}x"
    )
    return c_tc


def run(model_name: str, ort_optimize_first:bool=False):
    target: str = BenchmarkContext().target
    device:str=BenchmarkContext().device
    print(f"benchmark model: >>> {model_name} >>> ", end=": ")
    bert_onnx_model = get_backbone_onnx_path(model_name)
    output_path = Path(str(bert_onnx_model).replace(".onnx", "_aot.onnx"))
    lib_path = (
        Path(__file__).parent.resolve(strict=True) / f"lib{bert_onnx_model.stem}.so"
    )
    if output_path.exists() and lib_path.exists() and False:
        logger.debug("bypass compiling, use cached model")
    else:
        #fnn = ort_aot.debug_model(
        #    bert_onnx_model, output_path, lib_path, ort_optimize_first=ort_optimize_first)
        fnn = ort_aot.compile_model(bert_onnx_model, output_path, lib_path,  target=target,
                                    ort_optimize_first=ort_optimize_first)
    if target == "x86_64":
        verify_results(output_path, model_name, bert_onnx_model)
        c_tc = do_benchmark(output_path, bert_onnx_model, model_name)
    else:
        c_tc = bench_on_android.do_benchmark(
            output_path, bert_onnx_model, model_name, device, lib_path)
    Statistic(BenchMarkData(model_name, c_tc[0], c_tc[1], fnn))


def run_models_and_print_metrics():
    print("benchmarking models on PC...")
    #run("gpt2_dg", True)
    #run("nghuyong/ernie-1.0-base-zh")
    #run("valhalla/bart-large-sst2") xx
    run("squeezebert/squeezebert-uncased")
    run("gpt2")
    #run("gpt2", True)
    
    run("bert-base-uncased")
    #run("bert-base-uncased", True)
    #run("microsoft/deberta-base")   
    run("microsoft/deberta-base", True)   
    #run("google/mobilebert-uncased")
    run("google/mobilebert-uncased", True)
    #run("csarron/mobilebert-uncased-squad-v2")
    run("csarron/mobilebert-uncased-squad-v2", True)
    #run("lordtt13/emo-mobilebert")
    run("lordtt13/emo-mobilebert", True)
    #run("xlm-roberta-base")
    #run("xlm-roberta-base", True)
    #run("distilbert-base-uncased", True)
    st= Statistic()
    table_header = ['model_type', 'fused_nodes', 'ort_time', 'aot_time', 'speedup']
    table_data  = []
    for data in st.datas:
        #table_data .append((data.model_name, data.reduce_nodes, data.ort_time, data.aot_time, f"{(data.ort_time-data.aot_time)*100/data.ort_time:.6}%"))
        table_data.append((data.model_name, data.reduce_nodes, data.ort_time,
                          data.aot_time, f"{(data.ort_time/data.aot_time):.2f}x"))
    print(tabulate(table_data, headers=table_header, tablefmt='grid'))
    
    result = tabulate(table_data, headers=table_header, tablefmt='pipe')
    with open(Path(__file__).parent/'benchmark_results.md', 'w') as f:
        f.write(result)

def run_models_on_mobile_and_print_metrics():
    print("benchmarking models on android...")
    BenchmarkContext().target = "arm64-v8a"
    BenchmarkContext().device = "6e1e2521"
    #run("gpt2_dg", True)
    #run("nghuyong/ernie-1.0-base-zh")
    #run("valhalla/bart-large-sst2") xx
    run("squeezebert/squeezebert-uncased")
    run("gpt2")
    #run("gpt2", True)
    
    #run("bert-base-uncased")
    #run("bert-base-uncased", True)
    #run("microsoft/deberta-base")   
    #run("microsoft/deberta-base", True)   
    #run("google/mobilebert-uncased")
    run("google/mobilebert-uncased", True)
    #run("csarron/mobilebert-uncased-squad-v2")
    run("csarron/mobilebert-uncased-squad-v2", True)
    #run("lordtt13/emo-mobilebert")
    run("lordtt13/emo-mobilebert", True)
    #run("xlm-roberta-base")
    #run("xlm-roberta-base", True)
    run("distilbert-base-uncased", True)
    st= Statistic()
    table_header = ['model_type', 'fused_nodes', 'ort_time', 'aot_time', 'speedup']
    table_data  = []
    for data in st.datas:
        #table_data .append((data.model_name, data.reduce_nodes, data.ort_time, data.aot_time, f"{(data.ort_time-data.aot_time)*100/data.ort_time:.6}%"))
        table_data.append((data.model_name, data.reduce_nodes, data.ort_time,
                          data.aot_time, f"{(data.ort_time/data.aot_time):.2f}x"))
    print(tabulate(table_data, headers=table_header, tablefmt='grid'))
    
    result = tabulate(table_data, headers=table_header, tablefmt='pipe')
    with open(Path(__file__).parent/'benchmark_results.md', 'w') as f:
        f.write(result)

def main():
    parser = argparse.ArgumentParser(
        Path(__file__).parent.name,
        description=""" .
        """)
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Model type.",
    )
    args = parser.parse_args()
    if not args.verbose:
        logger.setLevel(ort_aot.logger.logging.WARNING)
     
    for i in range(1):
        run_models_and_print_metrics()

    
       

        

if __name__ == "__main__":
    main()

