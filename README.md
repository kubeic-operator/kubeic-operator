# Repository Coverage

[Full report](https://htmlpreview.github.io/?https://github.com/kubeic-operator/kubeic-operator/blob/python-coverage-comment-action-data/htmlcov/index.html)

| Name                                      |    Stmts |     Miss |   Cover |   Missing |
|------------------------------------------ | -------: | -------: | ------: | --------: |
| kubeic\_checker/\_\_init\_\_.py           |        0 |        0 |    100% |           |
| kubeic\_checker/availability.py           |      168 |       13 |     92% |80-81, 92-94, 120-125, 153-154 |
| kubeic\_checker/credentials.py            |       89 |        1 |     99% |        93 |
| kubeic\_checker/main.py                   |      220 |        8 |     96% |98, 372-373, 498-499, 526-528 |
| kubeic\_operator/\_\_init\_\_.py          |        0 |        0 |    100% |           |
| kubeic\_operator/checks/\_\_init\_\_.py   |        0 |        0 |    100% |           |
| kubeic\_operator/checks/prerelease.py     |       99 |        1 |     99% |       241 |
| kubeic\_operator/checks/spread.py         |       25 |        0 |    100% |           |
| kubeic\_operator/cleanup.py               |       26 |        1 |     96% |        58 |
| kubeic\_operator/deployer.py              |      304 |       17 |     94% |151, 158, 165-167, 170-171, 175-178, 594, 656, 667, 678, 689, 708, 841 |
| kubeic\_operator/handlers/\_\_init\_\_.py |        1 |        0 |    100% |           |
| kubeic\_operator/handlers/namespace.py    |       61 |        1 |     98% |        50 |
| kubeic\_operator/main.py                  |      265 |       48 |     82% |55, 135-145, 207-208, 262-264, 271, 281-282, 304-306, 337, 482, 488, 501-502, 515-517, 521-530, 539-552 |
| kubeic\_operator/metrics.py               |       56 |        0 |    100% |           |
| **TOTAL**                                 | **1314** |   **90** | **93%** |           |


## Setup coverage badge

Below are examples of the badges you can use in your main branch `README` file.

### Direct image

[![Coverage badge](https://raw.githubusercontent.com/kubeic-operator/kubeic-operator/python-coverage-comment-action-data/badge.svg)](https://htmlpreview.github.io/?https://github.com/kubeic-operator/kubeic-operator/blob/python-coverage-comment-action-data/htmlcov/index.html)

This is the one to use if your repository is private or if you don't want to customize anything.

### [Shields.io](https://shields.io) Json Endpoint

[![Coverage badge](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/kubeic-operator/kubeic-operator/python-coverage-comment-action-data/endpoint.json)](https://htmlpreview.github.io/?https://github.com/kubeic-operator/kubeic-operator/blob/python-coverage-comment-action-data/htmlcov/index.html)

Using this one will allow you to [customize](https://shields.io/endpoint) the look of your badge.
It won't work with private repositories. It won't be refreshed more than once per five minutes.

### [Shields.io](https://shields.io) Dynamic Badge

[![Coverage badge](https://img.shields.io/badge/dynamic/json?color=brightgreen&label=coverage&query=%24.message&url=https%3A%2F%2Fraw.githubusercontent.com%2Fkubeic-operator%2Fkubeic-operator%2Fpython-coverage-comment-action-data%2Fendpoint.json)](https://htmlpreview.github.io/?https://github.com/kubeic-operator/kubeic-operator/blob/python-coverage-comment-action-data/htmlcov/index.html)

This one will always be the same color. It won't work for private repos. I'm not even sure why we included it.

## What is that?

This branch is part of the
[python-coverage-comment-action](https://github.com/marketplace/actions/python-coverage-comment)
GitHub Action. All the files in this branch are automatically generated and may be
overwritten at any moment.