# Anonymised benchmark report

Measured runs: 16
Timing-valid runs: 11; calibration acceptance: failed

| ID | Trial | Pages | Total s | Calibration | IQR outlier |
| --- | ---: | ---: | ---: | --- | --- |
| B01 | 1 | 25 | 47.023 | fail | no |
| B01 | 2 | 25 | 56.144 | fail | no |
| B02 | 1 | 17 | 96.006 | fail | no |
| B02 | 2 | 17 | 84.033 | fail | no |
| B03 | 1 | 17 | 47.362 | fail | no |
| B03 | 2 | 17 | 115.688 | fail | no |
| B04 | 1 | 31 | 83.408 | fail | no |
| B04 | 2 | 31 | 34.878 | fail | no |
| B05 | 1 | 25 | 52.769 | fail | no |
| B05 | 2 | 25 | 111.154 | fail | no |
| B06 | 1 | 16 | 56.848 | fail | no |
| B06 | 2 | 16 | 51.558 | fail | no |
| B07 | 1 | 15 | 44.116 | fail | no |
| B07 | 2 | 15 | 19.698 | fail | no |
| B08 | 1 | 16 | 60.408 | fail | no |
| B08 | 2 | 16 | 47.516 | fail | no |

Duration (s): min 19.698, median 54.456, mean 63.038, max 115.688

## Trial-to-trial variance

| ID | Trials | Mean s | Min–max s | Variance s² |
| --- | ---: | ---: | --- | ---: |
| B01 | 2 | 51.584 | 47.023–56.144 | 41.596 |
| B02 | 2 | 90.019 | 84.033–96.006 | 71.676 |
| B03 | 2 | 81.525 | 47.362–115.688 | 2334.221 |
| B04 | 2 | 59.143 | 34.878–83.408 | 1177.58 |
| B05 | 2 | 81.962 | 52.769–111.154 | 1704.404 |
| B06 | 2 | 54.203 | 51.558–56.848 | 13.992 |
| B07 | 2 | 31.907 | 19.698–44.116 | 298.119 |
| B08 | 2 | 53.962 | 47.516–60.408 | 83.102 |

## Page-count groups (mean stage share)

| Group | Runs | Preparation | Submission | Shared ingestion | Validation | Reporting |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| lower | 6 | 22.284 | 0.458 | 60.87 | 10.725 | 2.361 |
| median | 4 | 19.082 | 0.224 | 57.77 | 19.788 | 1.377 |
| upper | 6 | 21.68 | 0.311 | 63.03 | 9.09 | 3.226 |
