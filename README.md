# Repository Coverage

[Full report](https://htmlpreview.github.io/?https://github.com/kubeic-operator/kubeic-operator/blob/python-coverage-comment-action-data/htmlcov/index.html)

| Name                                      |    Stmts |     Miss |   Cover |   Missing |
|------------------------------------------ | -------: | -------: | ------: | --------: |
| kubeic\_checker/\_\_init\_\_.py           |        0 |        0 |    100% |           |
| kubeic\_checker/availability.py           |      113 |       12 |     89% |66-67, 78-80, 98, 106-111 |
| kubeic\_checker/credentials.py            |       61 |        1 |     98% |        81 |
| kubeic\_checker/main.py                   |      132 |       47 |     64% |104, 112-115, 137, 142-143, 163-164, 173-226, 230-232 |
| kubeic\_operator/\_\_init\_\_.py          |        0 |        0 |    100% |           |
| kubeic\_operator/checks/\_\_init\_\_.py   |        0 |        0 |    100% |           |
| kubeic\_operator/checks/prerelease.py     |       99 |        1 |     99% |       241 |
| kubeic\_operator/checks/spread.py         |       25 |        0 |    100% |           |
| kubeic\_operator/cleanup.py               |       18 |        1 |     94% |        34 |
| kubeic\_operator/deployer.py              |      157 |       16 |     90% |46, 53, 60-62, 65-66, 70-73, 316, 327, 338, 349, 368, 388 |
| kubeic\_operator/handlers/\_\_init\_\_.py |        0 |        0 |    100% |           |
| kubeic\_operator/handlers/namespace.py    |       56 |        1 |     98% |        43 |
| kubeic\_operator/handlers/policy.py       |       18 |        0 |    100% |           |
| kubeic\_operator/main.py                  |      174 |       72 |     59% |36, 39, 42, 45, 48, 63, 66, 71-81, 85-109, 126-128, 143, 153-154, 160-161, 231, 237, 244-255, 264-278 |
| kubeic\_operator/metrics.py               |       38 |        0 |    100% |           |
| **TOTAL**                                 |  **891** |  **151** | **83%** |           |


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