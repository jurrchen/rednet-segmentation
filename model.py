import torch
from torch import nn
import math
import torch.utils.model_zoo as model_zoo
from torch.utils.checkpoint import checkpoint

class RedNetSeg(nn.Module):
  def __init__(self, num_classes=37):
    super(RedNetSeg, self).__init__()
    block = Bottleneck
    transblock = TransBasicBlock
    layers = [3, 4, 6, 3]
    # original resnet
    self.inplanes = 64
    self.conv1 = nn.Conv2d(3, 64, kernel_size=7, stride=2, padding=3,
                           bias=False)
    self.bn1 = nn.BatchNorm2d(64)
    self.relu = nn.ReLU(inplace=True)
    self.maxpool = nn.MaxPool2d(kernel_size=3, stride=2, padding=1)
    self.layer1 = self._make_layer(block, 64, layers[0])
    self.layer2 = self._make_layer(block, 128, layers[1], stride=2)
    self.layer3 = self._make_layer(block, 256, layers[2], stride=2)
    self.layer4 = self._make_layer(block, 512, layers[3], stride=2)

    # resnet for depth channel
    self.inplanes = 512
    self.deconv1 = self._make_transpose(transblock, 256, 6, stride=2)
    self.deconv2 = self._make_transpose(transblock, 128, 4, stride=2)
    self.deconv3 = self._make_transpose(transblock, 64, 3, stride=2)
    self.deconv4 = self._make_transpose(transblock, 64, 3, stride=2)

    self.agant0 = self._make_agant_layer(64, 64)
    self.agant1 = self._make_agant_layer(64 * 4, 64)
    self.agant2 = self._make_agant_layer(128 * 4, 128)
    self.agant3 = self._make_agant_layer(256 * 4, 256)
    self.agant4 = self._make_agant_layer(512 * 4, 512)

    # final block
    self.inplanes = 64
    self.final_conv = self._make_transpose(transblock, 64, 3)

    self.final_deconv = nn.ConvTranspose2d(self.inplanes, num_classes, kernel_size=2,
                                           stride=2, padding=0, bias=True)

    self.out5_conv = nn.Conv2d(256, num_classes, kernel_size=1, stride=1, bias=True)
    self.out4_conv = nn.Conv2d(128, num_classes, kernel_size=1, stride=1, bias=True)
    self.out3_conv = nn.Conv2d(64, num_classes, kernel_size=1, stride=1, bias=True)
    self.out2_conv = nn.Conv2d(64, num_classes, kernel_size=1, stride=1, bias=True)

    for m in self.modules():
        if isinstance(m, nn.Conv2d):
            n = m.kernel_size[0] * m.kernel_size[1] * m.out_channels
            m.weight.data.normal_(0, math.sqrt(2. / n))
        elif isinstance(m, nn.BatchNorm2d):
            m.weight.data.fill_(1)
            m.bias.data.zero_()

  def _make_layer(self, block, planes, blocks, stride=1):
      downsample = None
      if stride != 1 or self.inplanes != planes * block.expansion:
          downsample = nn.Sequential(
              nn.Conv2d(self.inplanes, planes * block.expansion,
                        kernel_size=1, stride=stride, bias=False),
              nn.BatchNorm2d(planes * block.expansion),
          )

      layers = []

      layers.append(block(self.inplanes, planes, stride, downsample))
      self.inplanes = planes * block.expansion
      for i in range(1, blocks):
          layers.append(block(self.inplanes, planes))

      return nn.Sequential(*layers)

  def _make_transpose(self, block, planes, blocks, stride=1):

      upsample = None
      if stride != 1:
          upsample = nn.Sequential(
              nn.ConvTranspose2d(self.inplanes, planes,
                                 kernel_size=2, stride=stride,
                                 padding=0, bias=False),
              nn.BatchNorm2d(planes),
          )
      elif self.inplanes != planes:
          upsample = nn.Sequential(
              nn.Conv2d(self.inplanes, planes,
                        kernel_size=1, stride=stride, bias=False),
              nn.BatchNorm2d(planes),
          )

      layers = []

      for i in range(1, blocks):
          layers.append(block(self.inplanes, self.inplanes))

      layers.append(block(self.inplanes, planes, stride, upsample))
      self.inplanes = planes

      return nn.Sequential(*layers)

  def _make_agant_layer(self, inplanes, planes):

    layers = nn.Sequential(
        nn.Conv2d(inplanes, planes, kernel_size=1,
                  stride=1, padding=0, bias=False),
        nn.BatchNorm2d(planes),
        nn.ReLU(inplace=True)
    )
    return layers

  def forward_downsample(self, rgb):

    x = self.conv1(rgb)
    x = self.bn1(x)
    x = self.relu(x)

    fuse0 = x

    x = self.maxpool(fuse0)

    # block 1
    x = self.layer1(x)
    fuse1 = x
    # block 2
    x = self.layer2(fuse1)
    fuse2 = x
    # block 3
    x = self.layer3(fuse2)
    fuse3 = x
    # block 4
    x = self.layer4(fuse3)
    fuse4 = x

    return fuse0, fuse1, fuse2, fuse3, fuse4

  def forward_upsample(self, fuse0, fuse1, fuse2, fuse3, fuse4):

    agant4 = self.agant4(fuse4)
    # upsample 1
    x = self.deconv1(agant4)
    if self.training:
        out5 = self.out5_conv(x)
    x = x + self.agant3(fuse3)
    # upsample 2
    x = self.deconv2(x)
    if self.training:
        out4 = self.out4_conv(x)
    x = x + self.agant2(fuse2)
    # upsample 3
    x = self.deconv3(x)
    if self.training:
        out3 = self.out3_conv(x)
    x = x + self.agant1(fuse1)
    # upsample 4
    x = self.deconv4(x)
    if self.training:
        out2 = self.out2_conv(x)
    x = x + self.agant0(fuse0)
    # final
    x = self.final_conv(x)
    out = self.final_deconv(x)

    if self.training:
      return out, out2, out3, out4, out5

    return out

  def forward(self, rgb):
    fuses = self.forward_downsample(rgb)
    out = self.forward_upsample(*fuses)

    return out

def conv3x3(in_planes, out_planes, stride=1):
    "3x3 convolution with padding"
    return nn.Conv2d(in_planes, out_planes, kernel_size=3, stride=stride,
                     padding=1, bias=False)


class Bottleneck(nn.Module):
  expansion = 4

  def __init__(self, inplanes, planes, stride=1, downsample=None):
    super(Bottleneck, self).__init__()
    self.conv1 = nn.Conv2d(inplanes, planes, kernel_size=1, bias=False)
    self.bn1 = nn.BatchNorm2d(planes)
    self.conv2 = nn.Conv2d(planes, planes, kernel_size=3, stride=stride,
                           padding=1, bias=False)
    self.bn2 = nn.BatchNorm2d(planes)
    self.conv3 = nn.Conv2d(planes, planes * 4, kernel_size=1, bias=False)
    self.bn3 = nn.BatchNorm2d(planes * 4)
    self.relu = nn.ReLU(inplace=True)
    self.downsample = downsample
    self.stride = stride

  def forward(self, x):
    residual = x

    out = self.conv1(x)
    out = self.bn1(out)
    out = self.relu(out)

    out = self.conv2(out)
    out = self.bn2(out)
    out = self.relu(out)

    out = self.conv3(out)
    out = self.bn3(out)

    if self.downsample is not None:
        residual = self.downsample(x)

    out += residual
    out = self.relu(out)

    return out

class TransBasicBlock(nn.Module):
  expansion = 1

  def __init__(self, inplanes, planes, stride=1, upsample=None, **kwargs):
    super(TransBasicBlock, self).__init__()
    self.conv1 = conv3x3(inplanes, inplanes)
    self.bn1 = nn.BatchNorm2d(inplanes)
    self.relu = nn.ReLU(inplace=True)
    if upsample is not None and stride != 1:
      self.conv2 = nn.ConvTranspose2d(inplanes, planes,
                                      kernel_size=3, stride=stride, padding=1,
                                      output_padding=1, bias=False)
    else:
      self.conv2 = conv3x3(inplanes, planes, stride)
    self.bn2 = nn.BatchNorm2d(planes)
    self.upsample = upsample
    self.stride = stride

  def forward(self, x):
    residual = x

    out = self.conv1(x)
    out = self.bn1(out)
    out = self.relu(out)

    out = self.conv2(out)
    out = self.bn2(out)

    if self.upsample is not None:
        residual = self.upsample(x)

    out += residual
    out = self.relu(out)

    return out


def rednetseg():
  return RedNetSeg(2)
