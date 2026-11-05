import os

import aws_cdk as cdk

from stacks.factory import add_stacks

app = cdk.App()
add_stacks(app, os.environ.get("CDK_DEFAULT_ACCOUNT"))
app.synth()
