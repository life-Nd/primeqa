import os
import json
import numpy as np
import joblib
from joblib import dump, load
import sklearn
from sklearn.neural_network import MLPClassifier
from oneqa.mrc.data_models.target_type import TargetType


class ConfidenceScorer(object):
    def __init__(self, confidence_model_path):
        if confidence_model_path:
            try:
                if os.path.isdir(confidence_model_path):
                    self._confidence_model = joblib.load(os.path.join(confidence_model_path, 'confidence_model.bin'))
                else:
                    self._confidence_model = joblib.load(confidence_model_path)
            except:
                raise ValueError("Unable to load confidence model from {}".format(confidence_model_path))
        else:
            self._confidence_model = None

    def model_exists(self):
        if self._confidence_model is not None:
            return True
        else:
            return False

    def make_features(self, example_predictions):
        # minimum risk f1
        minimum_risk_f1 = []
        for i in range(len(example_predictions)):
            sum = 0.0
            for j in range(len(example_predictions)):
                if j == i:
                    continue
                s1 = example_predictions[i]["span_answer"]["start_position"]
                e1 = example_predictions[i]["span_answer"]["end_position"]
                s2 = example_predictions[j]["span_answer"]["start_position"]
                e2 = example_predictions[j]["span_answer"]["end_position"]
                if s1 == s2 and e1 == e2:
                    sum += 1.0
                elif s1 > e2 or e1 < s2:
                    continue
                else:
                    overlap_start_position = max(s1, s2)
                    overlap_end_position = min(e1, e2)
                    precision = float(overlap_end_position - overlap_start_position + 1) / float(e1 - s1 + 1)
                    recall = float(overlap_end_position - overlap_start_position + 1) / float(e2 - s2 + 1)
                    f1 = (2 * precision * recall) / (precision + recall)
                    sum += f1
            sum /= len(example_predictions)
            minimum_risk_f1.append(sum)

        # if have span answer
        have_span_answer = []
        for pred in example_predictions:
            if "target_type_logits" not in pred:
                pred["target_type_logits"] = [0, 0, 0, 0, 0]
            if pred["target_type_logits"][TargetType.SPAN_ANSWER] == max(pred["target_type_logits"]):
                have_span_answer.append(1.0)
            else:
                have_span_answer.append(0.0)
        if max(have_span_answer) == 1.0:
            example_have_span_answer = 1.0
        else:
            example_have_span_answer = 0.0

        average_norm_span_answer_score = 0.0
        for pred in example_predictions:
            average_norm_span_answer_score += pred["normalized_span_answer_score"]
        average_norm_span_answer_score /= len(example_predictions)

        features = []
        for i, pred in enumerate(example_predictions):
            feat = []
            feat.append(pred["span_answer_score"])
            feat.append(pred["cls_score"])
            feat.append(pred["start_logit"])
            feat.append(pred["end_logit"])
            feat.append(pred["target_type_logits"][TargetType.NO_ANSWER])   # no answer
            feat.append(pred["target_type_logits"][TargetType.SPAN_ANSWER])   # span answer
            feat.append(have_span_answer[i])
            feat.append(example_have_span_answer)
            feat.append(minimum_risk_f1[i])
            feat.append(pred["normalized_span_answer_score"])
            feat.append(pred["normalized_span_answer_score"] - average_norm_span_answer_score)
            feat.append(pred["start_stdev"])
            feat.append(pred["end_stdev"])
            feat.append(pred["query_passage_similarity"])
            features.append(feat)
        return features

    def predict_scores(self, example_predictions):
        if not self.model_exists():
            return [0.0] * len(example_predictions)
        features = self.make_features(example_predictions)
        if len(features) == 0:
            return [0.0] * len(example_predictions)
        feature_dimension = len(features[0])
        X = np.zeros((len(features), feature_dimension), dtype=np.double)
        for i, feat in enumerate(features):
            X[i, :] = feat
        scores = self._confidence_model.predict_proba(X)
        # scores[:,0] : scores for incorrect, scores[:, 1]: score for correct
        return scores[:, 1]

    @classmethod
    def reference_prediction_overlap(cls, ground_truth, prediction):
        if not prediction or not ground_truth:
            return 0.0
        max_overlap_score = 0.0
        for truth in ground_truth:
            truth_start_position = truth["start_position"]
            truth_end_position = truth["end_position"]
            predicted_start_position = prediction["start_position"]
            predicted_end_position = prediction["end_position"]

            if truth_start_position == predicted_start_position and truth_end_position == predicted_end_position:
                return 1.0
            if truth_start_position < 0 or truth_end_position < 0:
                continue
            if predicted_start_position > truth_end_position or predicted_end_position < truth_start_position: # f1 = 0 since no overlap
                continue
            overlap_start_position = max(predicted_start_position, truth_start_position)
            overlap_end_position = min(predicted_end_position, truth_end_position)

            p = float(overlap_end_position - overlap_start_position + 1) / float(predicted_end_position - predicted_start_position + 1)
            r = float(overlap_end_position - overlap_start_position + 1) / float(truth_end_position - truth_start_position + 1)
            overlap_score= (2 * p * r) / (p + r)
            if max_overlap_score < overlap_score:
                max_overlap_score = overlap_score
        return max_overlap_score

    @classmethod
    def make_training_data(cls, prediction_file: str, reference_file: str, overlap_threshold: float = 0.5):
        try:
            with open(reference_file, 'r') as f:
                raw_references = json.load(f)
        except:
            raise ValueError("Unable to load reference file to create training data for confidence model")

        references = dict()
        for raw_ref in raw_references:
            example_id = raw_ref["example_id"][0]
            language = raw_ref["language"][0]
            ref = dict()
            ref["language"] = language
            ref["span_answer"] = []
            for i in range(len(raw_ref["start_position"])):
                span = dict()
                span["start_position"] = raw_ref["start_position"][i]
                span["end_position"] = raw_ref["end_position"][i]
                ref["span_answer"].append(span)
                references[example_id] = ref

        try:
            with open(prediction_file, 'r') as f:
                raw_predictions = json.load(f)
        except:
            raise ValueError("Unable to load prediction file to create training data for confidence model")

        feature_set = dict()
        label_set = dict()
        for example_id in raw_predictions:
            top_k_predictions = raw_predictions[example_id]

            # minimum risk f1
            minimum_risk_f1 = []
            for i in range(len(top_k_predictions)):
                sum = 0.0
                for j in range(len(top_k_predictions)):
                    if j == i:
                        continue
                    s1 = top_k_predictions[i]["span_answer"]["start_position"]
                    e1 = top_k_predictions[i]["span_answer"]["end_position"]
                    s2 = top_k_predictions[j]["span_answer"]["start_position"]
                    e2 = top_k_predictions[j]["span_answer"]["end_position"]
                    if s1 == s2 and e1 == e2:
                        sum += 1.0
                    elif s1 > e2 or e1 < s2:
                        continue
                    else:
                        overlap_start_position = max(s1, s2)
                        overlap_end_position = min(e1, e2)
                        precision = float(overlap_end_position - overlap_start_position + 1) / float(e1 - s1 + 1)
                        recall = float(overlap_end_position - overlap_start_position + 1) / float(e2 - s2 + 1)
                        f1 = (2 * precision * recall) / (precision + recall)
                        sum += f1
                sum /= len(top_k_predictions)
                minimum_risk_f1.append(sum)

            # if have span answer
            have_span_answer = []
            for pred in top_k_predictions:
                if "target_type_logits" not in pred:
                    pred["target_type_logits"] = [0, 0, 0, 0, 0]
                if pred["target_type_logits"][TargetType.SPAN_ANSWER] == max(pred["target_type_logits"]):
                    have_span_answer.append(1.0)
                else:
                    have_span_answer.append(0.0)
            if max(have_span_answer) == 1.0:
                example_have_span_answer = 1.0
            else:
                example_have_span_answer = 0.0

            average_norm_span_answer_score = 0.0
            for pred in top_k_predictions:
                average_norm_span_answer_score += pred["normalized_span_answer_score"]
            average_norm_span_answer_score /= len(top_k_predictions)

            feat = []
            pred = top_k_predictions[0]
            feat.append(pred["span_answer_score"])
            feat.append(pred["cls_score"])
            feat.append(pred["start_logit"])
            feat.append(pred["end_logit"])
            feat.append(pred["target_type_logits"][TargetType.NO_ANSWER])   # no answer
            feat.append(pred["target_type_logits"][TargetType.SPAN_ANSWER])   # span answer
            feat.append(have_span_answer[0])
            feat.append(example_have_span_answer)
            feat.append(minimum_risk_f1[0])
            feat.append(pred["normalized_span_answer_score"])
            feat.append(pred["normalized_span_answer_score"] - average_norm_span_answer_score)
            feat.append(pred["start_stdev"])
            feat.append(pred["end_stdev"])
            feat.append(pred["query_passage_similarity"])
            feature_set[example_id] = feat

            overlap_score = cls.reference_prediction_overlap(references[example_id]["span_answer"],
                                                              pred["span_answer"])
            if overlap_score >= overlap_threshold:
                label_set[example_id] = 1
            else:
                label_set[example_id] = 0

        for example_id in feature_set:
            number_features_per_example = len(feature_set[example_id])
            break
        X = np.zeros((len(feature_set), number_features_per_example), dtype=np.double)
        Y = np.zeros((len(feature_set)), dtype=np.int)

        for i, example_id in enumerate(sorted(feature_set.keys())):
            X[i, :] = feature_set[example_id]
            Y[i] = label_set[example_id]
        return X, Y
