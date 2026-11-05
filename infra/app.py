import os

import aws_cdk as cdk

from stacks.order_saga_stack import OrderSagaStack

app = cdk.App()
OrderSagaStack(
    app, "OrdersStepFunctions",
    env=cdk.Environment(account=os.environ.get("CDK_DEFAULT_ACCOUNT"), region="us-east-1"))
app.synth()
