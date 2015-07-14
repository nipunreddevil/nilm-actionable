import os
import time
import warnings

import pandas as pd
import nilmtk
from nilmtk import DataSet, MeterGroup
from nilmtk.disaggregate import CombinatorialOptimisation, FHMM, Hart85
from nilmtk.feature_detectors.steady_states import find_steady_states

warnings.filterwarnings("ignore")

import sys
import json
import glob

sys.path.append("../../code/fridge/")

if (len(sys.argv) < 2):
    ds_path = "/Users/nipunbatra/Downloads/wikienergy-2.h5"
else:
    ds_path = sys.argv[1]
with open('../../../data/fridge/top_k.json', 'r') as fp:
    top_k_dict = json.load(fp)

num_states = int(sys.argv[2])
K = int(sys.argv[3])
train_fraction = int(sys.argv[4]) / 100.0
classifier = sys.argv[5]
home_group = int(sys.argv[6])

print("*" * 80)
print("Arguments")

print("Number states", num_states)
print("Train fraction is ", train_fraction)
print("Top k", K)

script_path = os.path.dirname(os.path.realpath(__file__))
BASH_RUN_FRIDGE = os.path.join(script_path, "..", "bash_runs_fridge")

out_file_name = "N%d_K%d_T%s_%s" % (num_states, K, sys.argv[4], classifier)
OUTPUT_PATH = os.path.join(BASH_RUN_FRIDGE, out_file_name)

existing_files = glob.glob(OUTPUT_PATH + str("/*.h5"))
existing_files_names = [int(x.split("/")[-1].split(".")[0]) for x in existing_files]

ds = DataSet(ds_path)
fridges = nilmtk.global_meter_group.select_using_appliances(type='fridge')

fridges_id_building_id = {i: fridges.meters[i].building() for i in range(len(fridges.meters))}

fridge_id_building_id_ser = pd.Series(fridges_id_building_id)

from fridge_compressor_durations_optimised_jul_7 import compressor_powers

fridge_ids_to_consider = compressor_powers.keys()

building_ids_to_consider = fridge_id_building_id_ser[fridge_ids_to_consider]

NUM_CHUNKS = 30


def chunk_it(seq, num):
    avg = len(seq) / float(num)
    out = []
    last = 0.0

    while last < len(seq):
        out.append(seq[int(last):int(last + avg)])
        last += avg

    return out


def find_specific_appliance(appliance_name, appliance_instance, list_of_elecs):
    for elec_name in list_of_elecs:
        appl = elec_name.appliances[0]
        if (appl.identifier.type, appl.identifier.instance) == (appliance_name, appliance_instance):
            return elec_name


building_chunk_items = chunk_it(building_ids_to_consider.items(), NUM_CHUNKS)

out = {}

for f_id, b_id in building_chunk_items[home_group]:
    try:
        if f_id in existing_files_names:
            print("Skipping", f_id)
            continue

        print("*" * 80)
        print("Starting for ids %d and %d" % (f_id, b_id))
        print("*" * 80)
        start = time.time()
        out[f_id] = {}
        # Need to put it here to ensure that we have a new instance of the algorithm each time
        cls_dict = {"CO": CombinatorialOptimisation(), "FHMM": FHMM(), "Hart": Hart85()}
        elec = ds.buildings[b_id].elec
        mains = elec.mains()
        fridge_instance = fridges.meters[f_id].appliances[0].identifier.instance
        # Dividing train, test

        train = DataSet(ds_path)
        test = DataSet(ds_path)
        split_point = elec.train_test_split(train_fraction=train_fraction).date()
        train.set_window(end=split_point)
        test.set_window(start=split_point)
        train_elec = train.buildings[b_id].elec
        test_elec = test.buildings[b_id].elec
        test_mains = test_elec.mains()

        # Fridge elec
        fridge_elec_train = train_elec[('fridge', fridge_instance)]
        fridge_elec_test = test_elec[('fridge', fridge_instance)]

        num_states_dict = {fridge_elec_train: num_states}


        # Finding top N appliances
        top_k_train_list = top_k_dict[str(f_id)][:K]
        print("Top %d list is " % (K), top_k_train_list)
        top_k_train_elec = MeterGroup([m for m in ds.buildings[b_id].elec.meters if m.instance() in top_k_train_list])

        if not os.path.exists("%s/%s/" % (BASH_RUN_FRIDGE, out_file_name)):
            os.makedirs("%s/%s" % (BASH_RUN_FRIDGE, out_file_name))


        # Add this fridge to training if this fridge is not in top-k
        if fridge_elec_train not in top_k_train_elec.meters:
            top_k_train_elec.meters.append(fridge_elec_train)

        try:
            clf_name = classifier
            clf = cls_dict[clf_name]
            print("-" * 80)
            print("Training on %s" % clf_name)

            if clf_name == "Hart":
                fridge_df_train = fridge_elec_train.load().next()[('power', 'active')]
                fridge_power = fridge_df_train[fridge_df_train > 20]
                clf.train(train_elec.mains())
                d = (clf.centroids - fridge_power.mean()).abs()
                fridge_num = d[('power', 'active')].argmin()
                fridge_identifier_tuple = ('unknown', fridge_num)
            else:
                clf.train(top_k_train_elec, num_states_dict=num_states_dict)
                fridge_instance = fridges.meters[f_id].appliances[0].identifier.instance
                fridge_identifier_tuple = ('fridge', fridge_instance)

            print("-" * 80)
            print("Disaggregating")
            print("-" * 80)
            test_mains_df = test_mains.load().next()
            if clf_name == "Hart":
                [_, transients] = find_steady_states(test_mains_df, clf.cols,
                                                     clf.state_threshold, clf.noise_level)
                pred_df_fridge = clf.disaggregate_chunk(test_mains_df,
                                                        {}, transients)[[fridge_num]]

                pred_ser_fridge = pred_df_fridge.squeeze()
                pred_ser_fridge.name = "Hart"
                out[f_id][clf_name] = pred_ser_fridge
            elif clf_name == "CO":
                pred_df = clf.disaggregate_chunk(test_mains_df)
                pred_df.columns = [clf.model[i]['training_metadata'] for i in pred_df.columns]
                pred_df_fridge = pred_df[[find_specific_appliance('fridge',
                                                                  fridge_instance,
                                                                  pred_df.columns.tolist())]]
                pred_ser_fridge = pred_df_fridge.squeeze()
                pred_ser_fridge.name = "CO"
                out[f_id][clf_name] = pred_ser_fridge
            else:
                pred_df = clf.disaggregate_chunk(test_mains_df)
                pred_df_fridge = pred_df[[find_specific_appliance('fridge',
                                                                  fridge_instance,
                                                                  pred_df.columns.tolist())]]
                pred_ser_fridge = pred_df_fridge.squeeze()
                pred_ser_fridge.name = "FHMM"
                out[f_id][clf_name] = pred_ser_fridge

            fridge_df_test = fridge_elec_test.load().next()[('power', 'active')]
            fridge_df_test.name = "GT"
            out[f_id]["GT"] = fridge_df_test
            out_df = pd.DataFrame(out[f_id])
            print("Writing for fridge id: %d" % f_id)
            out_df.to_hdf("%s/%s/%d.h5" % (BASH_RUN_FRIDGE, out_file_name, f_id), "disag")

            end = time.time()
            time_taken = int(end - start)
            print "Id: %d took %d seconds" % (f_id, time_taken)
        except Exception, e:
            import traceback

            traceback.print_exc()
            print e
    except Exception, e:
        import traceback

        traceback.print_exc()
