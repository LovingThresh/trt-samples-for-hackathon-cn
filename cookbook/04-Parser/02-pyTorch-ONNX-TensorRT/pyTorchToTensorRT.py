#
# Copyright (c) 2021, NVIDIA CORPORATION. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

import os
import sys
import cv2
import numpy as np
from glob import glob
from datetime import datetime as dt
import torch as t
#import torchvision as tv           # 使用 pyTorch 默认的 MNIST 数据（含下载）
from torch.utils import data
import torch.nn.functional as F
from torch.autograd import Variable
from cuda import cuda
import tensorrt as trt
import calibrator
dataPath = os.path.dirname(os.path.realpath(__file__)) + "/../../00-MNISTData/"
sys.path.append( dataPath )
import loadMnistData

nTrainBatchSize     = 128
ptFile              = "./model.pt"
onnxFile            = "./model.onnx"
trtFile             = "./model.trt"
calibrationDataPath = dataPath + "test/"
cacheFile           = "./int8.cache"
calibrationCount    = 1
inputImage          = dataPath + "8.png"
imageHeight         = 28
imageWidth          = 28

class Net(t.nn.Module):
    def __init__(self):
        super(Net,self).__init__()
        self.conv1 = t.nn.Conv2d( 1, 32, (5,5), padding=(2,2), bias=True)
        self.conv2 = t.nn.Conv2d(32, 64, (5,5), padding=(2,2), bias=True)
        self.fc1 = t.nn.Linear(64*7*7, 1024, bias=True)
        self.fc2 = t.nn.Linear(  1024,   10, bias=True)

    def forward(self, x):
        x = F.max_pool2d(F.relu(self.conv1(x)), (2,2))
        x = F.max_pool2d(F.relu(self.conv2(x)), (2,2))
        x = x.reshape(-1,7*7*64)
        x = F.relu(self.fc1(x))
        y = self.fc2(x)
        z = F.softmax(y,dim=1)
        z = t.argmax(z,dim=1)
        return y,z

class MyData(data.Dataset):
    def __init__(self, path=dataPath, isTrain=True, nTrain=0, nTest=0):
        if isTrain:
            if len(glob(dataPath+"train/*.jpg")) == 0:
                mnist = loadMnistData.MnistData(path, isOneHot=False)
                mnist.saveImage([60000,nTrain][int(nTrain>0)], path+"train/", True)  # 60000 images in total
            self.data = glob(path+"train/*.jpg")
        else:
            if len(glob(dataPath+"test/*.jpg")) == 0:
                mnist = loadMnistData.MnistData(path, isOneHot=False)
                mnist.saveImage([10000,nTest][int(nTest>0)],   path+"test/",  False) # 10000 images in total
            self.data  = glob(path+"test/*.jpg")

    def __getitem__(self,index):
        imageName = self.data[index]
        data = cv2.imread(imageName,cv2.IMREAD_GRAYSCALE)
        label = np.zeros(10,dtype=np.float32)
        index = int(imageName[-7])
        label[index] = 1
        return t.from_numpy(data.reshape(1,imageHeight,imageWidth).astype(np.float32)), label

    def __len__(self):
        return len(self.data)

def runInPyTorch():
    if os.path.exists(ptFile):
        net = t.load(ptFile)
        print("Succeeded loading pt model!")
    else:
        net             = Net().cuda()
        ceLoss          = t.nn.CrossEntropyLoss()
        opt             = t.optim.Adam(net.parameters(),lr=0.001)
        #trainDataset    = tv.datasets.MNIST(root=".",train=True,transform=tv.transforms.ToTensor(),download=True)
        #testDataset     = tv.datasets.MNIST(root=".",train=False,transform=tv.transforms.ToTensor(),download=True)
        trainDataset    = MyData(isTrain=True,nTrain=3000)
        testDataset     = MyData(isTrain=False,nTest=500)
        trainLoader     = t.utils.data.DataLoader(dataset=trainDataset,batch_size=nTrainBatchSize,shuffle=True)
        testLoader      = t.utils.data.DataLoader(dataset=testDataset,batch_size=nTrainBatchSize,shuffle=True)

        for epoch in range(40):
            for i, (xTrain, yTrain) in enumerate(trainLoader):
                xTrain = Variable(xTrain).cuda()
                yTrain = Variable(yTrain).cuda()
                opt.zero_grad()
                y_,z = net(xTrain)
                loss = ceLoss(y_, yTrain)
                loss.backward()
                opt.step()
            if not (epoch+1)%10:
                print( "%s, epoch %d, loss = %f" %(dt.now(), epoch+1, loss.data) )

        t.save(net, ptFile)
        print("Succeeded building pt model!")

        acc = 0
        net.eval()
        for xTest, yTest in testLoader:
            xTest   = Variable(xTest).cuda()
            yTest   = Variable(yTest).cuda()
            y_,z    = net(xTest)
            acc    += t.sum(z == t.matmul(yTest,t.Tensor([0,1,2,3,4,5,6,7,8,9]).to('cuda:0'))).cpu().numpy()
        print( "test acc = %f"%(acc/len(testLoader)/nTrainBatchSize) )

    t.onnx.export(net,
                    t.randn(1, 1, imageHeight, imageWidth, device="cuda"),
                    "./model.onnx",
                    example_outputs=[t.randn(1,10,device="cuda"),t.randn(1,device="cuda")],
                    input_names=['x'],
                    output_names=['y','z'],
                    do_constant_folding=True,
                    verbose=True,
                    keep_initializers_as_inputs=True,
                    opset_version=12,
                    dynamic_axes={"x":{0:"nBatchSize"},"z":{0: "nBatchSize"}})

def runInTensorRT():
    logger = trt.Logger(trt.Logger.ERROR)
    if os.path.isfile(trtFile):
        with open(trtFile, 'rb') as f:
            engine = trt.Runtime(logger).deserialize_cuda_engine( f.read() )
        if engine == None:
            print("Failed loading engine!")
            return
        print("Succeeded loading engine!")
    else:
        builder                     = trt.Builder(logger)
        network                     = builder.create_network(1<<int(trt.NetworkDefinitionCreationFlag.EXPLICIT_BATCH))
        profile                     = builder.create_optimization_profile()
        config                      = builder.create_builder_config()
        config.flags                = 1<<int(trt.BuilderFlag.INT8)
        config.int8_calibrator      = calibrator.MyCalibrator(calibrationDataPath, calibrationCount, (1,1,imageHeight,imageWidth), cacheFile)
        config.max_workspace_size   = 3<<30
        parser = trt.OnnxParser(network, logger)
        if not os.path.exists(onnxFile):
            print("Failed finding onnx file!")
            return
        print("Succeeded finding onnx file!")
        with open(onnxFile, 'rb') as model:
            if not parser.parse( model.read() ):
                print ("Failed parsing ONNX file!")
                for error in range(parser.num_errors):
                    print (parser.get_error(error))
                return
            print ("Succeeded parsing ONNX file!")

        inputTensor = network.get_input(0)
        profile.set_shape(inputTensor.name, (1,1,28,28),(4,1,28,28),(16,1,28,28))
        config.add_optimization_profile(profile)

        network.unmark_output(network.get_output(0))    # 去掉输出张量 'y'
        engineString = builder.build_serialized_network(network,config)
        if engineString == None:
            print("Failed building engine!")
            return
        print("Succeeded building engine!")
        with open(trtFile, 'wb') as f:
            f.write( engineString )
        engine = trt.Runtime(logger).deserialize_cuda_engine( engineString )

    context = engine.create_execution_context()
    context.set_binding_shape(0,[1,1,28,28])
    _, stream   = cuda.cuStreamCreate(0)
    print("EngineBinding0->", engine.get_binding_shape(0), engine.get_binding_dtype(0));
    print("EngineBinding1->", engine.get_binding_shape(1), engine.get_binding_dtype(1));

    data        = cv2.imread(inputImage, cv2.IMREAD_GRAYSCALE).astype(np.float32)
    inputH0     = np.ascontiguousarray(data.reshape(-1))
    outputH0    = np.empty(context.get_binding_shape(1),dtype = trt.nptype(engine.get_binding_dtype(1)))
    _,inputD0   = cuda.cuMemAllocAsync(inputH0.nbytes,stream)
    _,outputD0  = cuda.cuMemAllocAsync(outputH0.nbytes,stream)

    cuda.cuMemcpyHtoDAsync(inputD0, inputH0.ctypes.data, inputH0.nbytes, stream)
    context.execute_async_v2([int(inputD0), int(outputD0)], stream)
    cuda.cuMemcpyDtoHAsync(outputH0.ctypes.data, outputD0, outputH0.nbytes, stream)
    cuda.cuStreamSynchronize(stream)

    print("inputH0 :", data.shape)
    #print(data)
    print("outputH0:", outputH0.shape)
    print(outputH0)

    cuda.cuStreamDestroy(stream)
    cuda.cuMemFree(inputD0)
    cuda.cuMemFree(outputD0)

if __name__ == '__main__':
    os.system("rm -rf ./*.pt ./*.onnx ./*.trt ./*.cache")
    cuda.cuInit(0)
    cuda.cuDeviceGet(0)
    np.set_printoptions(precision = 4, linewidth = 200, suppress = True)

    runInPyTorch()
    runInTensorRT()

    print("test finish!")

